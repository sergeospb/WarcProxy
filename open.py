import logging

import tornado

from tornado_proxy.proxy import run_proxy


logging.basicConfig(level=logging.DEBUG)
port = 8001
run_proxy(port, start_ioloop=False)

ili = tornado.ioloop.IOLoop.instance()
tornado.ioloop.PeriodicCallback(lambda: None, 500, ili).start()

logging.debug("Opening proxy on port %s" % port)
ili.start()