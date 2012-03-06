#
#   ztexworker - Integrating ztex 1.15x boards into MPBM
#
#   (c) 2012 nelisky.btc@gmail.com
#
#   This work is based upon the example worker bundled with MPBM and
#   the Java BTCMiner provided by ztex which is
#   Copyright (C) 2011 ZTEX GmbH
#   http://www.ztex.de
#   
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 3 as
#   published by the Free Software Foundation.
#
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, see http://www.gnu.org/licenses/.



################################

# TODO: count bad hashes
# TODO: freq setting algo based on error rate
# TODO: high speed configuration of fpga



# Module configuration options:
#   name: Display name for this work source (default: "SimpleRS232 on " port name)
#   jobinterval: New work is sent to the device at least every that many seconds (default: 30)


import sys
import common
import binascii
import threading
import time
import struct

from worker.fpgamining.ztexdev import scanDevices

def dataToInt (data):
  return struct.unpack('<I', data)[0]

def intToData (data):
  return struct.pack('<I', data)


class ZtexMinerHelper (object):
  def __init__ (self, dev, ep0force=False, cb=None):
    self.dev = dev
    self.numNonces = self.dev.getDescriptor().numNonces
    self.offsNonces = self.dev.getDescriptor().offsNonces
    self.goldenNonce = [0] * self.numNonces
    self.nonce = [0] * self.numNonces
    self.hash7 = [0] * self.numNonces
    self.overflowCount = 0
    self.lockFreq = True
    self._cb = cb

    self._checkcnt_goodtrigger = 1000
    
    self.dev.configureFpga(ep0force=ep0force)
    self.adjustFreq(0)

    self.ingoreErrorTime = 0

  def checkNonce (self, job, n, h):
    if time.time() < self.ignoreErrorTime:
      return True
    if self.lockFreq:
      return None
    rv = False
    t = (h+0x5be0cd19) & 0xFFFFFFFF
    for offs in (0,1,-1,2,-2):
      tn = intToData(n+offs)
      if struct.unpack('>I', job.gethash(tn)[28:32])[0] == t:
        self.checkcnt_good += 1
        rv = True
    if not rv:
      self.checkcnt_bad += 1
      if self.checkcnt_bad > 10 and self.checkcnt_good / self.checkcnt_bad < 33:
        self._checkcnt_goodtrigger *= 5
        self.adjustFreq(self.freqDelta-1)
    elif self.checkcnt_good > self._checkcnt_goodtrigger:
      if self.checkcnt_bad == 0:
        self.adjustFreq(self.freqDelta+1)
      else:
        self.checkcnt_bad = self.checkcnt_good = 0
    return rv

  def adjustFreq (self, f):
    self.freqDelta = f
    self.checkcnt_good = 0
    self.checkcnt_bad = 0
    self.dev._cmdSetFreq(self.dev.getDescriptor().freqM + f)
    self.ignoreErrorTime = time.time() + .25
    self._cb and self._cb(('adjustFreq', self.dev.getDescriptor().freqM1 * (self.dev.getDescriptor().freqM + f + 1)))

  def sendData (self, data):
    self.dev._cmdSendHashData(data)
    self.nonce = [0] * self.numNonces

  def getNoncesInt (self):
    buf = self.dev._reqReadHashData()
    overflow = False
    for i in range(self.numNonces):
      try:
        self.goldenNonce[i] = dataToInt(buf[i*12:i*12+4]) - self.offsNonces
        j = dataToInt(buf[i*12+4:i*12+8]) - self.offsNonces
        overflow = overflow or (((j >> 4) & 0xffffffff) < ((self.nonce[i]>>4) & 0xffffffff))
        self.nonce[i] = j
        self.hash7[i] = dataToInt(buf[i*12+8: i*12+12])
      except struct.error:
        # something wrong with the read data
        pass
    if overflow:
      self.overflowCount += 1

# Worker main class, referenced from config.py
class ZtexWorker(object):

  # Constructor, gets passed a reference to the miner core and the config dict for this worker
  def __init__(self, miner, dict, dev=None):

    # Make config dict entries accessible via self.foo
    self.__dict__ = dict

    # Store reference to the miner core object
    self.miner = miner
    
    # Initialize child array (we won't ever have any)
    self.children = []

    self.dead = False
    self.standalone = dev is None

    # Validate arguments, filling them with default values if not present
    self.serial = getattr(self, "serial", None)
    self.device = dev
    self.deviceid = getattr(self, "deviceid", "")
    self.basename = getattr(self, "name", "Ztex miner")
    self.name = self.basename
    self.jobinterval = getattr(self, "jobinterval", 30)
    self.jobspersecond = 1. / self.jobinterval  # Used by work buffering algorithm
    self.ep0force = getattr(self, "ep0force", False)

    # Initialize object properties (for statistics)
    self.mhps = 0          # Current MH/s
    self.mhashes = 0       # Total megahashes calculated since startup
    self.jobsaccepted = 0  # Total jobs accepted
    self.accepted = 0      # Number of accepted shares produced by this worker * difficulty
    self.rejected = 0      # Number of rejected shares produced by this worker * difficulty
    self.invalid = 0       # Number of invalid shares produced by this worker
    self.starttime = time.time()  # Start timestamp (to get average MH/s from MHashes)

    # Statistics lock, ensures that the UI can get a consistent statistics state
    # Needs to be acquired during all operations that affect the above values
    self.statlock = threading.RLock()

    # Placeholder for device response listener thread (will be started after synchronization)
    self.listenerthread = None

    # Initialize wakeup flag for the main thread
    self.wakeup = threading.Condition()

    # Start main thread (fetches work and pushes it to the device)
    self.mainthread = threading.Thread(None, self.main, self.name + "_main")
    self.mainthread.daemon = True
    self.mainthread.start()


  # Report statistics about this worker module and its (non-existant) children.
  def getstatistics(self, childstats):
    # Acquire the statistics lock to stop statistics from changing while we deal with them
    with self.statlock:
      # Calculate statistics
      statistics = { \
        "name": self.name, \
        "children": childstats, \
        "mhashes": self.mhashes, \
        "mhps": self.mhps, \
        "jobsaccepted": self.jobsaccepted, \
        "accepted": self.accepted, \
        "rejected": self.rejected, \
        "invalid": self.invalid, \
        "starttime": self.starttime, \
        "currentpool": self.job.pool.name if self.job != None and self.job.pool != None else None, \
      }
    # Return result
    return statistics


  # This function should interrupt processing of the current piece of work if possible.
  # If you can't, you'll likely get higher stale share rates.
  # This function is usually called when the work source gets a long poll response.
  # If we're currently doing work for a different blockchain, we don't need to care.
  def cancel(self, blockchain):
    # Get the wake lock to ensure that nobody else can change job/nextjob while we're checking.
    with self.wakeup:
      # Signal the main thread that it should get a new job if we're currently
      # processing work for the affected blockchain.
      if self.job != None and self.job.pool != None and self.job.pool.blockchain == blockchain:
        self.canceled = True
        self.wakeup.notify()
      # Check if an affected job is currently being uploaded.
      # If yes, it will be cancelled immediately after the upload.
      elif self.nextjob != None and self.nextjob.pool != None and self.nextjob.pool.blockchain == blockchain:
        self.canceled = True
        self.wakeup.notify()


  # Main thread entry point
  # This thread is responsible for fetching work and pushing it to the device.
  def main(self):
  
    # Loop forever. If anything fails, restart threads.
    while not self.dead:
      try:

        # Exception container: If an exception occurs in the listener thread, the listener thread
        # will store it here and terminate, and the main thread will rethrow it and then restart.
        self.error = None

        # Initialize megahashes per second to zero, will be measured later.
        self.mhps = 0

        # Job that the device is currently working on (found nonces are coming from this one).
        self.job = None

        # Job that is currently being uploaded to the device but not yet being processed.
        self.nextjob = None

        # Get handle for the serial port
        if self.device is None:
          devices = scanDevices(serial=self.serial)
          self.miner.log(self.name + ': Found %d device(s) with serial %s\n' % (len(devices), self.serial),"y")
          if not len(devices):
            time.sleep(1)
            continue
          self.device = devices[0]

        now = time.time()
        def cb (what):
          self.miner.log("-> %s\n" % str(what))
        self.device = ZtexMinerHelper(self.device, ep0force=self.ep0force, cb=cb)
        self.miner.log("Configuring %s-%d took %d ms\n" % (self.name, 1, int((time.time()-now)*1000)), "y")

        self.serial = self.device.dev.serial
        self.deviceid = self.device.dev.dev.iSerialNumber
        self.name = self.basename + '-%s-%d' % (self.serial, self.deviceid)
        
        # We keep control of the wakeup lock at all times unless we're sleeping
        self.wakeup.acquire()
        # Set validation success flag to false
        self.checksuccess = False
        # Initialize job cancellation (long poll) flag to false
        self.canceled = False

        # Start device response listener thread
        self.listenerthread = threading.Thread(None, self.listener, self.name + "_listener")
        self.listenerthread.daemon = True
        self.listenerthread.start()

        # Send validation job to device
        job = common.Job(self.miner, None, None, binascii.unhexlify(b"1625cbf1a5bc6ba648d1218441389e00a9dc79768a2fc6f2b79c70cf576febd0"), b"\0" * 64 + binascii.unhexlify(b"4c0afa494de837d81a269421"), None, binascii.unhexlify(b"7bc2b302"))
        self.sendjob(job)

        # Wait for validation job to be accepted by the device
        self.wakeup.wait(1)
        # If an exception occurred in the listener thread, rethrow it
        if self.error != None: raise self.error
        # If the job that was enqueued above has not been moved from nextjob to job by the
        # listener thread yet, something went wrong. Throw an exception to make everything restart.
        if self.nextjob != None: raise Exception("Timeout waiting for job ACK")

        # Wait for the validation job to complete. The wakeup flag will be set by the listener
        # thread when the validation job completes. 60 seconds should be sufficient for devices
        # down to about 760KH/s, for slower devices this timeout will need to be increased.
        self.wakeup.wait(60)
        # If an exception occurred in the listener thread, rethrow it
        if self.error != None: raise self.error
        # We woke up, but the validation job hasn't succeeded in the mean time.
        # This usually means that the wakeup timeout has expired.
        if not self.checksuccess: raise Exception("Timeout waiting for validation job to finish")
        # self.mhps has now been populated by the listener thread
        self.miner.log(self.name + ": Running at %f MH/s\n" % self.mhps, "B")
        # Calculate the time that the device will need to process 2**32 nonces.
        # This is limited at 30 seconds so that new transactions can be included into the block
        # by the work source. (Requirement of the bitcoin protocol and enforced by most pools.)
        interval = min(30, 2**32 / 1000000. / self.mhps)
        # Add some safety margin and take user's interval setting (if present) into account.
        self.jobinterval = min(self.jobinterval, max(0.5, interval * 0.8 - 1))
        self.miner.log(self.name + ": Job interval: %f seconds\n" % self.jobinterval, "B")
        # Tell the MPBM core that our hash rate has changed, so that it can adjust its work buffer.
        self.jobspersecond = 1. / self.jobinterval
        self.miner.updatehashrate(self)
        self.device.lockFreq = False
        # Main loop, continues until something goes wrong.
        while True:

          # Fetch a job. Blocks until one is available. Because of this we need to release the
          # wake lock temporarily in order to avoid possible deadlocks.
          self.canceled = False
          self.wakeup.release()
          job = self.miner.getjob(self)
          # Doesn't need acquisition of the statlock because we're the only one who modifies this.
          self.jobsaccepted = self.jobsaccepted + 1
          self.wakeup.acquire()
          
          # If a new block was found while we were fetching that job,
          # check the long poll epoch to verify that the work that we got isn't stale.
          # If it is, just discard it and get a new one.
          if self.canceled == True:
            if job.longpollepoch != job.pool.blockchain.longpollepoch: continue
          self.canceled = False

          # If an exception occurred in the listener thread, rethrow it
          if self.error != None: raise self.error

          # Upload the piece of work to the device
          self.sendjob(job)
          # Wait for up to one second for the device to accept it
          self.wakeup.wait(1)
          # If an exception occurred in the listener thread, rethrow it
          if self.error != None: raise self.error
          # If the job that was send above has not been moved from nextjob to job by the listener
          # thread yet, something went wrong. Throw an exception to make everything restart.
          if self.nextjob != None: raise Exception("Timeout waiting for job ACK")
          # If the job was already caught by a long poll while we were uploading it,
          # jump back to the beginning of the main loop in order to immediately fetch new work.
          # Don't check for the canceled flag before the job was accepted by the device,
          # otherwise we might get out of sync.
          if self.canceled: continue
          # Wait while the device is processing the job. If nonces are sent by the device, they
          # will be processed by the listener thread. If a long poll comes in, we will be woken up.
          self.wakeup.wait(self.jobinterval)
          # If an exception occurred in the listener thread, rethrow it
          if self.error != None: raise self.error

      # If something went wrong...
      except Exception as e:
        import traceback
        self.miner.log(traceback.format_exc()+ '\n')
        # ...complain about it!
        self.miner.log(self.name + ": %s\n" % e, "rB")
        # Make sure that the listener thread realizes that something went wrong
        self.error = e
        # We're not doing productive work any more, update stats
        self.mhps = 0
        # Release the wake lock to allow the listener thread to move. Ignore it if that goes wrong.
        try: self.wakeup.release()
        except: pass
        # Wait for the listener thread to terminate.
        # If it doens't within 10 seconds, continue anyway. We can't do much about that.
        try: self.listenerthread.join(10)
        except: pass
        # Set MH/s to zero again, the listener thread might have overwritten that.
        self.mhps = 0
        # Wait for a second to avoid 100% CPU load if something fails reproducibly
        time.sleep(1)
        if self.standalone:
          self.device = None
        else:
          self.dead = True
        # Restart (handled by "while True:" loop above)


  # Device response listener thread
  def listener(self):

    # Catch all exceptions and forward them to the main thread
    try:
      nonces = []
      ovfcnt = 0
      # Loop forever unless something goes wrong
      while True:

        # If the main thread has a problem, make sure we die before it restarts
        if self.error != None: break

        if self.nextjob:
          # Send it to the device
          with self.wakeup:
            self.job = self.nextjob
            self.nextjob = None
            sendBuf = self.job.data[64:76] + self.job.state
            self.device.sendData(sendBuf)
            self.job.starttime = time.time()
            self.wakeup.notify()
            continue

        # Try to read a response from the device
        data = ''

        self.device.getNoncesInt()

        for n in self.device.goldenNonce:
          if n > 0 and n not in nonces:
            data = intToData(n)
            nonces.append(n)
            nonces = nonces[-(len(self.device.goldenNonce)*1):]
            break
        
        if len(data) == 0:
          if self.device.overflowCount > ovfcnt:
            ovfcnt = self.device.overflowCount
            # The device managed to process the whole 2**32 keyspace before we sent it new work.
            self.miner.log(self.name + " exhausted keyspace!\n", "y")
            # If it was a validation job, this probably means that there is a hardware bug
            # or that the "found share" message was lost on the communication channel.
            if self.job.check != None: raise Exception("Validation job terminated without finding a share")
            # Stop measuring time because the device is doing duplicate work right now
            if self.job != None and self.job.starttime != None and self.job.pool != None:
              mhashes = (time.time() - self.job.starttime) * self.mhps
              with self.job.pool.statlock: self.job.pool.mhashes = self.job.pool.mhashes + mhashes
              self.mhashes = self.mhashes + mhashes
              #self.job.starttime = None
            # Wake up the main thread to fetch new work ASAP.
            with self.wakeup: self.wakeup.notify()
            continue
          if self.nextjob == None and self.device.nonce[0] > 0:
            t = self.device.checkNonce(self.job, self.device.nonce[0], self.device.hash7[0])
            if not t:
              self.miner.log('checkNonce: %s, nonce: %d\n' % (t, self.device.nonce[0]))
            time.sleep(0.025)
        else:
          # We found a share!
          nonce = data
          if self.job == None: raise Exception("Mining device sent a share before even getting a job")
          now = time.time()
          self.job.sendresult(nonce, self)
          delta = (now - self.job.starttime)
          self.mhps = self.device.nonce[0] / 1000000. / delta
          self.miner.updatehashrate(self)
          if self.job.check != None:
            # This is a validation job. Validate that the nonce is correct, and complain if not.
            if self.job.check != nonce:
              raise Exception("Mining device is not working correctly (returned %s instead of %s)" % (binascii.hexlify(nonce).decode("ascii"), binascii.hexlify(self.job.check).decode("ascii")))
            else:
              # The nonce was correct. Wake up the main thread.
              with self.wakeup:
                self.checksuccess = True
                self.wakeup.notify()
          continue

    # If an exception is thrown in the listener thread...
    except Exception as e:
      # ...put it into the exception container...
      import traceback
      self.miner.log(traceback.format_exc()+ '\n')
      self.error = e
      # ...wake up the main thread...
      with self.wakeup: self.wakeup.notify()
      # ...and terminate the listener thread.


  # This function uploads a job to the device
  def sendjob(self, job):
    # Put it into nextjob. It will be moved to job by the listener
    # thread as soon as it gets acknowledged by the device.
    self.nextjob = job
    #self.handle.write(struct.pack("B", 1) + job.state[::-1] + job.data[75:63:-1])
