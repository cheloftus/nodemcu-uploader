#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (C) 2015-2016 Peter Magnusson <peter@birchroad.net>

import time
import logging
import hashlib
import os
from platform import system
import serial

from .luacode import SAVE_LUA

log = logging.getLogger(__name__)

__all__ = ['Uploader', 'default_port']

CHUNK_END = '\v'
CHUNK_REPLY = '\v'

def default_port():
    return {
        'Windows': 'COM1',
        'Darwin': '/dev/tty.SLAB_USBtoUART'
    }.get(system(), '/dev/ttyUSB0')

class Uploader(object):
    BAUD = 9600
    TIMEOUT = 5
    PORT = default_port()

    def __init__(self, port=PORT, baud=BAUD):
        self._port = serial.Serial(port, Uploader.BAUD, timeout=Uploader.TIMEOUT)

        # Keeps things working, if following conections are made:
        ## RTS = CH_PD (i.e reset)
        ## DTR = GPIO0
        self._port.setRTS(False)
        self._port.setDTR(False)

        # Get in sync with LUA (this assumes that NodeMCU gets reset by the previous two lines)
        self.exchange(';') # Get a defined state
        self.writeln('print("%sync%");')
        self.expect('%sync%\r\n> ')

        if baud != Uploader.BAUD:
            log.info('Changing communication to %s baud', baud)
            self.writeln('uart.setup(0,%s,8,0,1,1)' % baud)

            # Wait for the string to be sent before switching baud
            time.sleep(0.1)
            self._port.setBaudrate(baud)

            # Get in sync again
            self.exchange('')
            self.exchange('')

        self.line_number = 0

    def expect(self, exp='> ', timeout=TIMEOUT):
        timer = self._port.timeout

        # Checking for new data every 100us is fast enough
        lt = 0.0001
        if self._port.timeout != lt:
            self._port.timeout = lt

        end = time.time() + timeout

        # Finish as soon as either exp matches or we run out of time (work like dump, but faster on success)
        data = ''
        while not data.endswith(exp) and time.time() <= end:
            data += self._port.read()

        self._port.timeout = timer
        log.debug('expect return: %s', data)
        return data

    def write(self, output, binary=False):
        if not binary:
            log.debug('write: %s', output)
        else:
            log.debug('write binary: %s', ':'.join(x.encode('hex') for x in output))
        self._port.write(output)
        self._port.flush()

    def writeln(self, output):
        self.write(output + '\n')

    def exchange(self, output):
        self.writeln(output)
        return self.expect()



    def close(self):
        self.writeln('uart.setup(0,%s,8,0,1,1)' % Uploader.BAUD)
        self._port.close()

    def prepare(self):
        log.info('Preparing esp for transfer.')

        data = SAVE_LUA.replace('9600', '%d' % self._port.baudrate)
        lines = data.replace('\r', '').split('\n')

        for line in lines:
            line = line.strip().replace(', ', ',').replace(' = ', '=')

            if len(line) == 0:
                continue

            d = self.exchange(line)
            #do some basic test of the result
            if 'unexpected' in d or len(d) > len(SAVE_LUA)+10:
                log.error('error in save_lua "%s"', d)
                return

    def download_file(self, filename):
        chunk_size = 256
        bytes_read = 0
        data = ""
        while True:
            d = self.exchange("file.open('" + filename + r"') print(file.seek('end', 0)) file.seek('set', %d) uart.write(0, file.read(%d))file.close()" % (bytes_read, chunk_size))
            cmd, size, tmp_data = d.split('\n', 2)
            data = data + tmp_data[0:chunk_size]
            bytes_read = bytes_read + chunk_size
            if bytes_read > int(size):
                break
        data = data[0:int(size)]
        return data

    def read_file(self, filename, destination=''):
        if not destination:
            destination = filename
        log.info('Transfering %s to %s', filename, destination)
        data = self.download_file(filename)
        with open(destination, 'w') as f:
            f.write(data)

    def write_file(self, path, destination='', verify='none'):
        filename = os.path.basename(path)
        if not destination:
            destination = filename
        log.info('Transfering %s as %s', path, destination)
        self.writeln("recv()")

        res = self.expect('C> ')
        if not res.endswith('C> '):
            log.error('Error waiting for esp "%s"', res)
            return
        log.debug('sending destination filename "%s"', destination)
        self.write(destination + '\x00', True)
        if not self.got_ack():
            log.error('did not ack destination filename')
            return

        f = open(path, 'rb')
        content = f.read()
        f.close()

        log.debug('sending %d bytes in %s', len(content), filename)
        pos = 0
        chunk_size = 128
        while pos < len(content):
            rest = len(content) - pos
            if rest > chunk_size:
                rest = chunk_size

            data = content[pos:pos+rest]
            if not self.write_chunk(data):
                d = self.expect()
                log.error('Bad chunk response "%s" %s', d, ':'.join(x.encode('hex') for x in d))
                return

            pos += chunk_size

        log.debug('sending zero block')
        #zero size block
        self.write_chunk('')

        if verify == 'standard':
            log.info('Verifying...')
            data = self.download_file(destination)
            if content != data:
                log.error('Verification failed.')
        elif verify == 'sha1':
            #Calculate SHA1 on remote file. Extract just hash from result
            data = self.exchange('shafile("'+destination+'")').splitlines()[1]
            log.info('Remote SHA1: %s', data)

            #Calculate hash of local data
            filehashhex = hashlib.sha1(content.encode()).hexdigest()
            log.info('Local SHA1: %s', filehashhex)
            if data != filehashhex:
                log.error('Verification failed.')

    def exec_file(self, path):
        filename = os.path.basename(path)
        log.info('Execute %s', filename)

        f = open(path, 'rt')

        res = '> '
        for line in f:
            line = line.rstrip('\r\n')
            retlines = (res + self.exchange(line)).splitlines()
            # Log all but the last line
            res = retlines.pop()
            for lin in retlines:
                log.info(lin)
        # last line
        log.info(res)
        f.close()

    def got_ack(self):
        log.debug('waiting for ack')
        res = self._port.read(1)
        log.debug('ack read %s', res.encode('hex'))
        return res == '\x06' #ACK


    def write_lines(self, data):
        lines = data.replace('\r', '').split('\n')

        for line in lines:
            self.exchange(line)

        return


    def write_chunk(self, chunk):
        log.debug('writing %d bytes chunk', len(chunk))
        data = '\x01' + chr(len(chunk)) + chunk
        if len(chunk) < 128:
            padding = 128 - len(chunk)
            log.debug('pad with %d characters', padding)
            data = data + (' ' * padding)
        log.debug("packet size %d", len(data))
        self.write(data)

        return self.got_ack()


    def file_list(self):
        log.info('Listing files')
        res = self.exchange('for key,value in pairs(file.list()) do print(key,value) end')
        log.info(res)
        return res

    def file_do(self, f):
        log.info('Executing '+f)
        res = self.exchange('dofile("'+f+'")')
        log.info(res)
        return res

    def file_format(self):
        log.info('Formating...')
        res = self.exchange('file.format()')
        if 'format done' not in res:
            log.error(res)
        else:
            log.info(res)
        return res

    def node_heap(self):
        log.info('Heap')
        res = self.exchange('print(node.heap())')
        log.info(res)
        return res

    def node_restart(self):
        log.info('Restart')
        res = self.exchange('node.restart()')
        log.info(res)
        return res

    def file_compile(self, path):
        log.info('Compile '+path)
        cmd = 'node.compile("%s")' % path
        res = self.exchange(cmd)
        log.info(res)
        return res

    def file_remove(self, path):
        log.info('Remove '+path)
        cmd = 'file.remove("%s")' % path
        res = self.exchange(cmd)
        log.info(res)
        return res
