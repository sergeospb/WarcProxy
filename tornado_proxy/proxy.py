#!/usr/bin/env python
#
# Simple asynchronous HTTP proxy with tunnelling (CONNECT).
#
# GET/POST proxying based on
# http://groups.google.com/group/python-tornado/msg/7bea08e7a049cf26
#
# Copyright (C) 2012 Senko Rasic <senko.rasic@dobarkod.hr>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import sys
import socket
import hashlib
import cPickle
import zlib
import base64
from  cStringIO import StringIO
import weakref

import tornado.httpserver
import tornado.ioloop
import tornado.iostream
import tornado.web
import tornado.httpclient
from scrapy.utils.url import canonicalize_url

import tornadoasyncmemcache as memcache


ccs = memcache.ClientPool(['127.0.0.1:11211'], maxclients=5000)

__all__ = ['ProxyHandler', 'run_proxy']
from  tornado.httpclient import HTTPResponse

CACHED_CODES = [200, 301, 302, 303, 307, 404, ]

_fingerprint_cache = weakref.WeakKeyDictionary()


def fingerprint_request(req, arguments=None):
    """
    from scrapy
    Return the request fingerprint.

    The request fingerprint is a hash that uniquely identifies the resource the
    request points to. For example, take the following two urls:

    http://www.example.com/query?id=111&cat=222
    http://www.example.com/query?cat=222&id=111

    Even though those are two different URLs both point to the same resource
    and are equivalent (ie. they should return the same response).

    """
    cache = _fingerprint_cache.setdefault(req, {})
    url = canonicalize_url(req.url)
    if url not in cache:
        fp = hashlib.sha1()
        fp.update(str(url))
        fp.update(str(req.method))
        fp.update(str(req.body))
        if arguments:
            for name, value in arguments.iteritems():
                fp.update("%s%s" % (name, value))
        cache[url] = fp.hexdigest()
        #pdb.set_trace()
    return cache[url]


def serialize_response(response):
    result = {
        'body': response.body,
        'code': response.code,
        'effective_url': response.effective_url,
        'headers': response.headers,
        'time_info': response.time_info,
        'request_time': response.request_time,
    }

    serialized = cPickle.dumps(result)
    return base64.encodestring(zlib.compress(serialized))


def unserialize_response(dumped, request):
    serialized = zlib.decompress(base64.decodestring(dumped))
    result = cPickle.loads(serialized)
    buffer = StringIO()
    buffer.write(result['body'])
    #response.buffer = buffer
    response = HTTPResponse(
        request=request,
        effective_url=result['effective_url'],
        code=result['code'],
        request_time=result['request_time'],
        headers=result['headers'],
        time_info=result['time_info'],
        buffer=buffer,
    )

    return response


class ProxyHandler(tornado.web.RequestHandler):
    SUPPORTED_METHODS = ['GET', 'POST', 'CONNECT']

    def initialize(self):
        tornado.httpclient.AsyncHTTPClient.configure("tornado_proxy.warc_httpclient.WarcSimpleAsyncHTTPClient")

    @tornado.web.asynchronous
    def get(self):
        self._memcached = False

        def handle_response(response):
            if response.error and not isinstance(response.error,
                                                 tornado.httpclient.HTTPError):
                self.set_status(500)
                self.write('Internal server error:\n' + str(response.error))
                self.finish()
            else:
                self.set_status(response.code)
                for header in ('Date', 'Cache-Control', 'Server',
                               'Content-Type', 'Location'):
                    v = response.headers.get(header)
                    if v:
                        self.set_header(header, v)
                if response.body:
                    self.write(response.body)
                if not self._memcached and response.code in CACHED_CODES:
                    def mem_set(data):
                        pass

                    dumped = serialize_response(response)
                    ccs.set(self.fingerprint, dumped, callback=mem_set)
                self.finish()

        #http://www.squid-cache.org/Doc/config/read_timeout/ 15 min
        #http://www.squid-cache.org/Doc/config/connect_timeout/ 1 min
        req = tornado.httpclient.HTTPRequest(url=self.request.uri,
                                             method=self.request.method, body=self.request.body,
                                             headers=self.request.headers, follow_redirects=False,
                                             allow_nonstandard_methods=True,
                                             connect_timeout=float(1 * 50), request_timeout=float(15 * 60),
        )
        self.fingerprint = fingerprint_request(req, self.request.arguments)

        def mem_get(dumped):
            if not dumped:
                client = tornado.httpclient.AsyncHTTPClient(max_clients=5000)
                try:
                    client.fetch(req, handle_response)
                except tornado.httpclient.HTTPError, e:
                    if hasattr(e, 'response') and e.response:
                        self.handle_response(e.response)
                    else:
                        self.set_status(500)
                        self.write('Internal server error:\n' + str(e))
                        self.finish()
            else:
                response = unserialize_response(dumped, req)
                self._memcached = True
                handle_response(response)
                #pdb.set_trace()

        ccs.get(self.fingerprint, callback=mem_get)


    @tornado.web.asynchronous
    def post(self):
        return self.get()

    @tornado.web.asynchronous
    def connect(self):
        host, port = self.request.uri.split(':')
        client = self.request.connection.stream

        def read_from_client(data):
            upstream.write(data)

        def read_from_upstream(data):
            client.write(data)

        def client_close(data=None):
            if upstream.closed():
                return
            if data:
                upstream.write(data)
            upstream.close()

        def upstream_close(data=None):
            if client.closed():
                return
            if data:
                client.write(data)
            client.close()

        def start_tunnel():
            client.read_until_close(client_close, read_from_client)
            upstream.read_until_close(upstream_close, read_from_upstream)
            client.write('HTTP/1.0 200 Connection established\r\n\r\n')

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        upstream = tornado.iostream.IOStream(s)
        upstream.connect((host, int(port)), start_tunnel)


def run_proxy(port, start_ioloop=True):
    """
    Run proxy on the specified port. If start_ioloop is True (default),
    the tornado IOLoop will be started immediately.
    """

    app = tornado.web.Application([
                                      (r'.*', ProxyHandler),
                                  ], debug=True)
    app.listen(port)
    ioloop = tornado.ioloop.IOLoop.instance()
    if start_ioloop:
        ioloop.start()


if __name__ == '__main__':
    port = 8888
    if len(sys.argv) > 1:
        port = int(sys.argv[1])

    print "Starting HTTP proxy on port %d" % port
    run_proxy(port)
