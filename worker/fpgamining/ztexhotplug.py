#
#   ztexhotplug - Manage spawning workers for ztex 1.15x boards
#
#   (c) 2012 nelisky.btc@gmail.com
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as
#   published by the Free Software Foundation.
#
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#   General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, see http://www.gnu.org/licenses/.
#
##
#
# Inspiration and a few snippets shamelessly stolen from the x6500hotplug worker
#


# Module configuration options:
#   name: Display name for this work source
#   jobinterval: New work is sent to the device at least every that many seconds (default: 30)

import sys
import common
import binascii
import threading
import time
import struct

from worker.fpgamining.ztexdev import scanDevices
from worker.fpgamining.ztexworker import ZtexWorker

# Worker main class, referenced from config.py
class ZtexHotplug(object):

  # Constructor, gets passed a reference to the miner core and the config dict for this worker
  def __init__(self, miner, dict, dev=None):

    # Make config dict entries accessible via self.foo
    self.__dict__ = dict

    # Store reference to the miner core object
    self.miner = miner
    
    # Initialize child array
    self.children = []

    # Validate arguments, filling them with default values if not present
    self.serial = getattr(self, "serial", None)
    self.name = getattr(self, "name", "Ztex hotplug manager")
    self.scaninterval = getattr(self, "scaninterval", 10)
    self.jobinterval = getattr(self, "jobinterval", 30)
    self.jobspersecond = 1. / self.jobinterval  # Used by work buffering algorithm

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


  # Report statistics about this worker module and its children.
  def getstatistics(self, childstats):
    # Acquire the statistics lock to stop statistics from changing while we deal with them
    with self.statlock:
      # Calculate statistics
      statistics = { \
        "name": self.name, \
        "children": childstats, \
        "mhashes": self.mhashes + self.miner.calculatefieldsum(childstats, "mhashes"), \
        "mhps": self.miner.calculatefieldsum(childstats, "mhps"), \
        "jobsaccepted": self.jobsaccepted + self.miner.calculatefieldsum(childstats, "jobsaccepted"), \
        "accepted": self.accepted + self.miner.calculatefieldsum(childstats, "accepted"), \
        "rejected": self.rejected + self.miner.calculatefieldsum(childstats, "rejected"), \
        "invalid": self.invalid + self.miner.calculatefieldsum(childstats, "invalid"), \
        "starttime": self.starttime, \
        "currentpool": "Not applicable", \
      }
    # Return result
    return statistics
    
  # This function should interrupt processing of the current piece of work if possible.
  # If you can't, you'll likely get higher stale share rates.
  # This function is usually called when the work source gets a long poll response.
  # If we're currently doing work for a different blockchain, we don't need to care.
  def cancel(self, blockchain):
    # Check all running children
    for child in self.children:
      # Forward the request to the child
      child.cancel(blockchain)

  # Main thread entry point
  # This thread is responsible for fetching work and pushing it to the device.
  def main(self):
  
    while True:
      try:
        for child in self.children:
          if child.dead:
            with self.statlock:
              stats = child.getstatistics(self.miner.collectstatistics(child.children))
              self.children.remove(child)
              self.mhashes = self.mhashes + stats["mhashes"]
              self.jobsaccepted = self.jobsaccepted + stats["jobsaccepted"]
              self.accepted = self.accepted + stats["accepted"]
              self.rejected = self.rejected + stats["rejected"]
              self.invalid = self.invalid + stats["invalid"]
        childmap = [x.deviceid for x in self.children]
        for dev in scanDevices(serial=self.serial):
          if dev.dev.iSerialNumber not in childmap:
            config = { \
              "deviceid": dev.dev.iSerialNumber, \
              "serial": self.serial, \
              "name": "Ztex miner", \
              "jobinterval": self.jobinterval, \
            }
            self.children.append(ZtexWorker(self.miner, config, dev))
      except Exception as e:
        self.miner.log("Caught exception: %s\n" % e, "r")
      time.sleep(self.scaninterval)
        
