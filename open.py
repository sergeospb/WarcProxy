import logging
import os

import tornado

from tornado_proxy.proxy import run_proxy

filename = os.path.abspath('logs/proxy.log')
if not os.path.exists(os.path.dirname(filename)):
    os.mkdir(os.path.dirname(filename))
logging.basicConfig(
    filename=filename,
    filemode='w',
    format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
    level=logging.DEBUG,
)
port = 8001
run_proxy(port, start_ioloop=False)

ili = tornado.ioloop.IOLoop.instance()
tornado.ioloop.PeriodicCallback(lambda: None, 500, ili).start()

logging.debug("Opening proxy on port %s" % port)
ili.start()