#
#   ztexdev - basic python support for Ztex 1.15x fpga board
#
#   (c) 2012 nelisky.btc@gmail.com
#
#   This work is based upon the Java SDK provided by ztex which is
#   Copyright (C) 2009-2011 ZTEX GmbH.
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

import usb
import struct
import array
import time
import logging

logger = logging.getLogger('ztex')

import os
HERE = os.path.abspath(os.path.dirname(__file__))

## Cypress vendor ID: 0x4b4
cypressVendorId = 0x4b4
## EZ-USB USB product ID: 0x8613
cypressProductId = 0x8613

## ZTEX vendor ID: 0x221a
ztexVendorId = 0x221A
## 
 # USB product ID for ZTEX devices that support ZTEX descriptor 1: 0x100.
 # This product ID is intended for general purpose use and can be shared by all devices that base on ZTEX modules.
 # Different products are identified by a second product ID, namely the PRODUCT_ID field of the
 # <a href="#descriptor"> ZTEX descr iptor 1</a>.
 # <p>
 # Please read the <a href="http://www.ztex.de/firmware-kit/usb_ids.e.html">informations about USB vendor and product ID's<a>.
 # @see #ztexProductIdMax
##
ztexProductId = 0x100
##
 # Largest USB product ID for ZTEX devices that support ZTEX descriptor 1: 0x1ff.
 # USB product ID's from {@link #ztexProductId}+1 to ztexProductIdMax (0x101 to 0x1ff) are reserved for ZTEX
 # devices and allow to identify products without reading the ZTEX descriptor.
 # <p>
 # Please read the <a href="http://www.ztex.de/firmware-kit/usb_ids.e.html">informations about USB vendor and product ID's<a>.
 # @see #ztexProductId
##
ztexProductIdMax = 0x1ff

capabilityMap = {
    # Capability index for EEPROM support.
    "CAPABILITY_EEPROM": 0,
    # Capability index for FPGA configuration support.
    "CAPABILITY_FPGA": 1,
    # Capability index for FLASH memory support.
    "CAPABILITY_FLASH": 2,
    # Capability index for DEBUG helper support.
    "CAPABILITY_DEBUG": 3,
    # Capability index for AVR XMEGA support.
    "CAPABILITY_XMEGA": 4,
    # Capability index for AVR XMEGA support.
    "CAPABILITY_HS_FPGA": 5,
    # Capability index for AVR XMEGA support.
    "CAPABILITY_MAC_EEPROM": 6,
    }

capabilityStrings = [
    "EEPROM read/write" ,
    "FPGA configuration" ,
    "Flash memory support",
    "Debug helper",
    "XMEGA support", 
    "High speed FPGA configuration",
    "MAC EEPROM read/write" 
    ]
    


class DeviceNotSupportedException (Exception):
    pass

class InvalidFirmwareException (Exception):
    pass

class CapabilityException (Exception):
    pass

class AlreadyConfiguredException (Exception):
    pass

class BitstreamUploadException (Exception):
    pass

class Ztex (object):
    def __init__ (self, dev):
        self.dev = dev
        self.desc = None
        self.serial = None
        self.manufacturerString = None
        self.productString = None
        self.__prepare__()

    def __prepare__ (self):
        self.getDescriptor()
        usbVendorId = self.dev.idVendor & 65535
        usbProductId = self.dev.idProduct & 65535
        if not ((usbVendorId == ztexVendorId) and
                (usbProductId == ztexProductId)):
            raise DeviceNotSupportedException("%0.4X:%0.4X" % (usbVendorId, usbProductId))
        
        self.manufacturerString = self._getString(self.dev.iManufacturer)
        self.productString = self._getString(self.dev.iProduct)
        self.getSerial()

        if self.serial is None:
            raise InvalidFirmwareException("Not a Ztex device -> %0.4X:%0.4X" % (usbVendorId, usbProductId))

        buf = self._vendorRequest(0x22, 40)
        if len(buf) != 40:
            raise InvalidFirmwareException("Error reading ZTEX descriptor: read %d bytes" % len(buf))
        if buf[:6].tostring() != '\x28\x01ZTEX':
            raise InvalidFirmwareException("Invalid ZTEX descriptor: '%s'" % str(buf))
        self.productId = buf[6:10].tolist()
        self.fwVersion = buf[10]
        self.interfaceVersion = buf[11]
        self.interfaceCapabilities = buf[12:18].tolist()
        self.moduleReserved = buf[18:30].tolist()

        if not (self.productId[0] == 10 and self.productId[2:4] == [1,1]):
            raise InvalidFirmwareException("Wrong or no firmware")
        self.interfaceClaimed = [False] * 256

    def getDescriptor (self, force=False):
        if self.desc is None or force:
            self.desc = Descriptor(self)
        return self.desc

    def getFpgaState (self):
        self.checkCapability("CAPABILITY_FPGA")
        buf = self._reqGetFpgaState()
        rv = {'fpgaConfigured': buf[0] == 0,
              'fpgaChecksum': buf[1] & 0xff,
              'fpgaBytes': ((buf[5] & 0xff)<<24) | ((buf[4] & 0xff)<<16) | ((buf[3] & 0xff)<<8) | (buf[2] & 0xff),
              'fpgaInitB': buf[6] & 0xff,
              'fpgaFlashResult': buf[7],
              'fpgaFlashBitSwap': buf[8] != 0}
        return rv
        
    def _getString (self, idx):
        if idx > 0:
            return usb.util.get_string(self.dev, 256, idx).replace('\0','')
        return None
  
    def getSerial (self):
        if self.serial is None:
            self.serial = self._getString(self.dev.iSerialNumber)
        return self.serial
  
    def isValid (self):
        # we are not allowing the class to be instanced unless we can read descriptor 1 so
        return True

    def checkCapability (self, i, j=0):
        """
        Checks whether ZTEX descriptor 1 is available and interface 1 and a given capability are supported.
        
        @param i byte index of the capability
        @param j bit index of the capability
        """
        if type(i) == type(''):
            t = capabilityMap[i]
            i = t/8
            j = t%8
        if  not (i<len(self.interfaceCapabilities) and j<8 and 
                 (self.interfaceCapabilities[i] & (1<<j)) != 0):
            k = i*8+j
            raise CapabilityException( k < len(capabilityStrings) and capabilityStrings[k] or ("Capabilty " + i + "," + j) ) 

    def resetFpga (self):
	self.checkCapability("CAPABILITY_FPGA")
        self._vendorCommand(0x31)

    def detectBitstreamBitOrder (self, buf):
        p1 = buf.find('\xaa\x99\x55\x66')
        p2 = buf.find('\x55\x99\xaa\x66')
        if p1 >= 0 and p2 >=0:
            return p1 < p2 and 1 or 0
        if p1 >= 0:
            return 1
        if p2 < 0:
            logger.warn('Unable to determine bitstream bit order: no signature found')
        return 0

    def swapBits (self, buf):
        def doswap (b):
            b = ord(b)
            return chr(((b & 128) >> 7) | \
                       ((b &  64) >> 5) | \
                       ((b &  32) >> 3) | \
                       ((b &  16) >> 1) | \
                       ((b &   8) << 1) | \
                       ((b &   4) << 3) | \
                       ((b &   2) << 5) | \
                       ((b &   1) << 7))
        return map(doswap, buf)

    def getFpgaConfiguration (self):
        return self.getFpgaState()['fpgaConfigured']

    def configureFpga (self, ep0force=False):
        try:
            if ep0force:
                return self.configureFpgaLS(os.path.join(HERE, 'bitstreams',self.desc.bitFileName+'.bit'), True, 2)
            return self.configureFpgaHS(os.path.join(HERE, 'bitstreams',self.desc.bitFileName+'.bit'), True, 2)
        except:
            return self.configureFpgaLS(os.path.join(HERE, 'bitstreams',self.desc.bitFileName+'.bit'), True, 2)

    def configureFpgaHS (self, firmware, force, bs):
        """
        Upload a Bitstream to the FPGA using high speed mode.
        
        @param fwFileName The file name of the Bitstream. The file can be a regular file or a system resource (e.g. a file from the current jar archive).
        @param force If set to true existing configurations will be overwritten. (By default an {@link AlreadyConfiguredException} is thrown).
        @param bs 0: disable bit swapping, 1: enable bit swapping, all other values: automatic detection of bit order.
        """
	self.checkCapability("CAPABILITY_HS_FPGA")
        settings = self._reqGetHSFpgaSettings()

        if (not force and self.getFpgaConfiguration()):
            raise AlreadyConfiguredException()
        
        ep = settings[0] & 255
        iface = settings[1] & 255
        releaseIF = not self.interfaceClaimed[iface]
        
        transactionBytes = 65536
        buf = open(firmware, 'rb').read()
        if len(buf) < 64:
            raise BitstreamReadException("Invalid file size: %d" % len(buf))
        
        if bs < 0 or  bs > 1:
	    bs = self.detectBitstreamBitOrder (buf)
	if bs == 1:
	    buf = self.swapBits(buf)

        if releaseIF:
            usb.util.claim_interface(self.dev, iface)
            self.interfaceClaimed[iface] = True

        for tries in range(3, 0, -1):
            self._cmdInitHSFPGAConfiguration()
            p = 0
            cs = 0
            while p < len(buf):
                sent = self._bulkWrite(ep, buf[p:p+transactionBytes], iface, 10000)
                p += sent
            self._cmdFinishHSFPGAConfiguration()
            state = self.getFpgaState()
            try:
                if not state['fpgaConfigured']:
                    raise BitstreamUploadException("FPGA configuration failed: DONE pin does not go high")
            except BitstreamUploadException, x:
                if tries > 1:
                    logger.error(str(x) + ': Retrying it...')
                else:
                    raise
            else:
                break
        if releaseIF:
            usb.util.release_interface(self.dev, iface)
            self.interfaceClaimed[iface] = False

    def configureFpgaLS (self, firmware, force, bs):
        """
        Upload a Bitstream to the FPGA using low speed mode.

        @param fwFileName The file name of the Bitstream. The file can be a regular file or a system resource (e.g. a file from the current jar archive).
        @param force If set to true existing configurations will be overwritten.
        @param bs 0: disable bit swapping, 1: enable bit swapping, all other values: automatic detection of bit order.
        """
	self.checkCapability("CAPABILITY_FPGA")
	
        if (not force and self.getFpgaConfiguration()):
            raise AlreadyConfiguredException() 

        transactionBytes = 2048
        buf = open(firmware, 'rb').read()
        # ensure size % 64 == 0
        buf += '\0' * (len(buf) % 64)
        # detect bitstream bit order and swap bits if necessary 
        if bs < 0 or  bs > 1:
	    bs = self.detectBitstreamBitOrder (buf)
	if bs == 1:
	    buf = self.swapBits(buf)
        for tries in range(10, 0, -1):
            self.resetFpga()
            p = 0
            cs = 0
            while p < len(buf):
                sent = self._cmdSendFpgaData(buf[p:p+transactionBytes])
                p += sent
                cs = (cs + sum(map(lambda x: ord(x), buf[p:p+sent]))) & 0xFF
            state = self.getFpgaState()
            try:
                if not state['fpgaConfigured']:
                    state['cs'] = cs
                    raise BitstreamUploadException("FPGA configuration failed: DONE pin does not go high (size=%(fpgaBytes)s); checksum=%(fpgaChecksum)s should be %(cs)s; INIT_B_HIST=%(fpgaInitB)s)" % state)
            except BitstreamUploadException, x:
                if tries > 1:
                    logger.error(str(x) + ': Retrying it...')
                else:
                    raise
            else:
                break

    def _reqReadHashData (self):
        numNonces = self.getDescriptor().numNonces
        return ''.join([chr(x) for x in self._vendorRequest(0x81,maxlen=numNonces*12)])
  
    def _cmdSendFpgaData (self, data):
        return self._vendorCommand(0x32, data=data)
  
    def _cmdInitHSFPGAConfiguration (self):
        return self._vendorCommand(0x34)
  
    def _cmdFinishHSFPGAConfiguration (self):
        return self._vendorCommand(0x35)
  
    def _cmdSendHashData (self, data):
        return self._vendorCommand(0x80, data=data)
  
    def _cmdSetFreq (self, m):
        d = self.getDescriptor()
        if m > d.freqMaxM:
            m = d.freqMaxM
        return self._vendorCommand(0x83, value=m)
      
    def _reqReadDescriptor (self):
        return self._vendorRequest(0x82,maxlen=64)

    def _reqGetFpgaState (self):
        return self._vendorRequest(0x30,maxlen=9)
  
    def _reqGetHSFpgaSettings (self):
        return self._vendorRequest(0x33,maxlen=2)

    def _bulkWrite (self, ep, data, iface=None, timeout=None):
        if type(data) == type('') or (type(data) == type([]) and type(data[0]) == type('')):
            data  = [ord(x) for x in data]
        if data is None:
            data = []
        return self.dev.write(ep, data, iface, timeout=timeout)
  
    def _vendorCommand (self, cmd, value=0, index=0, data=None):
        if type(data) == type('') or (type(data) == type([]) and type(data[0]) == type('')):
            data  = [ord(x) for x in data]
        if data is None:
            data = []
        #print "sending %0.2X, %0.2X, %0.4X, %0.4X, %s" % (0x40, cmd, value, index, data)
        rv = self.dev.ctrl_transfer(0x40, cmd, value, index, data, timeout=5000)
        return rv
  
    def _vendorRequest (self, cmd, value=0, index=0, maxlen=256):
        return self.dev.ctrl_transfer(0xc0,cmd,value,index,maxlen,timeout=5000) 

class Descriptor (object):
  def __init__ (self, dev):
    buf = dev._reqReadDescriptor()
    r = struct.unpack_from('<BBHHBBH', buf)
    self.numNonces = r[1]+1
    self.offsNonces = r[2] - 10000
    self.freqM1 = r[3] * 0.01
    self.freqM = r[4]
    self.freqMaxM = r[5]
    if self.freqM > self.freqMaxM:
      self.freqM = self.freqMaxM

    self.freqMDefault = self.freqM
    if r[0] == 4:
      self.hashesPerClock = (r[6]+1) / 128.0
    else:
      self.hashesPerClock = 1.0

    i0 = r[0] == 4 and 10 or 8
    for i in range(i0, 64):
      if buf[i] == 0:
        break

    if i < (i0+1):
      logger.error("invalid bitstream file name")
    else:
      self.bitFileName =  buf.tostring()[i0:i]

    if r[0] != 4:
      if self.bitFileName[:13] == 'ztex_ufm1_15b':
        self.hashesPerClock = 0.5
      logger.warn("Warning: HASHES_PER_CLOCK not defined, assuming %0.2f" % self.hashesPerClock)

  def __str__ (self):
    return "bitfile=%s   f_default=%0.2fMHz  f_max=%0.2fMHz  HpC=%0.1fH" % (self.bitFileName, self.freqM1 * (self.freqMDefault+1), self.freqM1 * (self.freqMaxM+1), self.hashesPerClock)

def scanDevices (serial=None):
  devs = usb.core.find(find_all=True, idVendor=0x221a, idProduct=0x0100)
  for d in devs:
    try:
      d.set_configuration()
    except usb.core.USBError:
      continue
  rv = [Ztex(x) for x in devs]
  if serial is not None:
    rv = filter(lambda x: x.getSerial() == serial, rv)
  return rv

if __name__ == '__main__':
    d = scanDevices()
