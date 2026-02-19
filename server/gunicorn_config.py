import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('SERVER_PORT', '80')}"
cpu_count = multiprocessing.cpu_count()
workers = int(os.getenv('GUNICORN_WORKERS', cpu_count * 2 + 1))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = int(os.getenv('GUNICORN_WORKER_CONNECTIONS', '2000'))
max_requests = int(os.getenv('GUNICORN_MAX_REQUESTS', '1000'))
max_requests_jitter = int(os.getenv('GUNICORN_MAX_REQUESTS_JITTER', '50'))
timeout = int(os.getenv('GUNICORN_TIMEOUT', '300'))  # Increased for WebSocket: 5 minutes
keepalive = int(os.getenv('GUNICORN_KEEPALIVE', '10'))  # Increased keepalive
graceful_timeout = int(os.getenv('GUNICORN_GRACEFUL_TIMEOUT', '30'))

preload_app = True
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

accesslog = "-"
errorlog = "-"
loglevel = os.getenv('LOG_LEVEL', 'info').lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

proc_name = "ratpanel_server"

def on_starting(server):
    server.log.info("Starting RATPanel Server with Gunicorn")

def on_reload(server):
    server.log.info("Reloading RATPanel Server")

def worker_int(worker):
    worker.log.info("Worker received INT or QUIT signal")

def pre_fork(server, worker):
    pass

def post_fork(server, worker):
    server.log.info("Worker spawned (pid: %s)", worker.pid)

def post_worker_init(worker):
    worker.log.info("Worker initialized (pid: %s)", worker.pid)

def worker_abort(worker):
    worker.log.warning("Worker received SIGABRT signal")

def on_exit(server):
    server.log.info("Shutting down RATPanel Server")

