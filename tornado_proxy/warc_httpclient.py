from __future__ import absolute_import, division, with_statement
import functools
import os.path
import datetime
import anydbm
from  cStringIO import StringIO
import httplib
import hashlib

from tornado import stack_context
from tornado.simple_httpclient import SimpleAsyncHTTPClient, _HTTPConnection

import warc

"""
Singleton that handles maintaining a single output file for many connections

"""
import urlparse
import re
import logging

REGEXP_HOST = re.compile("[^\.]+\.[^\.]+$")


def get_hostname(url):
    hostname = urlparse.urlparse(url).hostname
    hostname = re.findall(REGEXP_HOST, hostname)[0]
    return hostname


class WarcWriter(object):
    warc_fp_slots = {}
    warc_file_n_slots = {}

    def __init__(self, outdir='result'):
        max_mb_size = 100
        self.max_size = max_mb_size * 1024 * 1024
        self.outdir = outdir
        if not os.path.exists(self.outdir):
            os.mkdir(self.outdir)
        now = datetime.datetime.now().strftime('%Y-%m-%d_%H:%M:%S')
        self.outdir = os.path.join(self.outdir, now)
        if not os.path.exists(self.outdir):
            os.mkdir(self.outdir)

        self.now_iso_format = WarcWriter.now_iso_format()
        if not os.path.exists(self.outdir):
            os.mkdir(self.outdir)
        self.warc_dir = os.path.join(self.outdir, 'warc')
        if not os.path.exists(self.warc_dir):
            os.mkdir(self.warc_dir)
        self.db_index_dir = os.path.join(self.outdir, 'db_index')
        if not os.path.exists(self.db_index_dir):
            os.mkdir(self.db_index_dir)

        db_fname = os.path.join(self.db_index_dir, 'index.db')
        self.db = anydbm.open(db_fname, 'n')
        #self.file_n = 0
        #self.warc_fp = None
        self.fname_prefix = ""
        self.hostname = None
        #self._get_warc_file()

    @staticmethod
    def now_iso_format():
        '''Returns a string with the current time according to the ISO8601 format'''
        now = datetime.datetime.utcnow()
        return now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def write_record(self, headers, content, response_url, http_code):
        hash_url = hashlib.md5(str(response_url)).hexdigest()
        if hash_url in self.db:
            logging.debug('Response url in db %s' % response_url)
            return
        self.db[hash_url] = '1'
        logging.debug('Response url not in db %s' % response_url)
        #Content-Encoding: gzip
        self.hostname = get_hostname(response_url)
        payload = StringIO()

        status_reason = httplib.responses.get(http_code, '-')
        payload.write('HTTP/1.1 %d %s\r\n' % (http_code, status_reason))
        for h_name in headers:
            payload.write('%s: %s\n' % (h_name, headers[h_name]))
        payload.write('\r\n')
        payload.write(content)
        headers = {
            'WARC-Type': 'response',
            'WARC-Date': self.now_iso_format,
            'Content-Length': str(payload.tell()),
            'Content-Type': str(headers.get('Content-Type', '')),
            'WARC-Target-URI': response_url,
        }
        record = warc.WARCRecord(payload=payload.getvalue(), headers=headers)
        self._write_record(record)


    def _write_record(self, record):
        '''Writes a record in the current Warc file.

        If the current file exceeds the limit defined by `self.max_size`, the
        file is closed and a new one is created.
        '''
        #pdb.set_trace()
        warc_fp = self.warc_fp_slots.get(self.hostname)
        if not warc_fp:
            warc_fp = self._get_warc_file()
        warc_fp.write_record(record)
        #pdb.set_trace()
        #warc_fp.fileobj.flush()

        curr_pos = warc_fp.tell()
        if curr_pos > self.max_size:
            warc_fp.close()
            #self.warc_fp_slots[self.hostname]
            self._get_warc_file()

    def _get_warc_file(self):
        '''Creates a new Warc file'''
        #assert self.warc_fp is None, 'Current Warc file must be None'
        if not self.hostname:
            return
        file_n = self.warc_file_n_slots.get(self.hostname)
        if not file_n:
            file_n = 0
        file_n += 1
        self.warc_file_n_slots[self.hostname] = file_n
        fname = '%s_%s.warc.gz' % (self.hostname, file_n)
        warc_fname = os.path.join(self.warc_dir, fname)
        assert os.path.exists(warc_fname) is not True
        warc_fp = warc.open(warc_fname, 'w')
        self.warc_fp_slots[self.hostname] = warc_fp
        return warc_fp


warc_writer = WarcWriter()


class Warc_HTTPConnection(_HTTPConnection, object):
    """
    """

    def _run_callback(self, response):
        if response.headers.get('Transfer-Encoding'):
            del response.headers['Transfer-Encoding']
        if response.headers.get('Content-Encoding'):
            del response.headers['Content-Encoding']
        warc_writer.write_record(
            headers=response.headers, content=response.body,
            http_code=response.code, response_url=response.effective_url,
        )
        super(Warc_HTTPConnection, self)._run_callback(response)


class WarcSimpleAsyncHTTPClient(SimpleAsyncHTTPClient):
    def __init__(self, *args, **kwargs):
        #self._warcout = WarcOutputSingleton()
        SimpleAsyncHTTPClient.__init__(self, *args, **kwargs)

    def _process_queue(self):
        with stack_context.NullContext():
            while self.queue and len(self.active) < self.max_clients:
                request, callback = self.queue.popleft()
                key = object()
                self.active[key] = (request, callback)
                Warc_HTTPConnection(self.io_loop, self, request,
                                    functools.partial(self._release_fetch, key),
                                    callback,
                                    self.max_buffer_size)