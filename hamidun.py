#!/usr/bin/env python
"""
Hamidun is stupid and ugly load balancer for docker based services.

Please read README.md
"""

import os
import re
import time
from os import listdir, environ
from os.path import isfile, join
from threading import Thread
from docker import Client as DockerClient


__author__ = "Abi Hafshin"
__copyright__ = "Copyright 2016 Abi Hafshin"
__license__ = "MIT"
__version__ = "0.1.0"
__maintainer__ = "Abi Hafshin"
__email__ = "abi@hafs.in"
__status__ = "Development"


LABEL_UPSTREAM_NAME = 'org.hamidun.name'
LABEL_UPSTREAM_PORT = 'org.hamidun.port'

UPSTREAM_FILE_NAME = '000-upstream.conf'
NGINX_DEFAULT_DOWN_PORT = 13579

MODIFIED_MIN_DELTA = 5


def main():
  docker_host = get_env('DOCKER_HOST', 'unix://var/run/docker.sock')
  docker = DockerClient(base_url=docker_host)

  network_name = get_env('DOCKER_NETWORK_NAME', 'load_balancer')
  network = Network(docker, network_name)

  hamidun = Hamidun(docker, network)
  
  config_writer = ConfigurationWriter()
  config_writer.down_port = get_env('NGINX_DOWN_PORT', NGINX_DEFAULT_DOWN_PORT)

  template_dir = get_env('TEMPLATES_DIR', '/etc/nginx/sites-template')
  out_dir = get_env('OUTPUT_DIR', '/etc/nginx/conf.d/')
  files = [f for f in listdir(template_dir) if isfile(join(template_dir, f))]
  for f in files:
    config_writer.process_template_file(join(template_dir, f), join(out_dir, f))
  
  upstream_file = join(out_dir, UPSTREAM_FILE_NAME)
  config_writer.upstreams = hamidun.read_docker()
  config_writer.write_upstream(upstream_file)
  
  monitor_thread = MonitorThread(hamidun, config_writer, upstream_file)
  monitor_thread.start()
  for upstreams in hamidun.monitor_docker():
    config_writer.upstreams = upstreams
    monitor_thread.notify_changed()
  # finish

def get_env(name, default):
  """return environment variable named name, if it does not exist, return default"""
  if name in environ:
    return environ[name]
  else:
    return default



class Hamidun:
  def __init__(self, docker, network):
    self.docker = docker
    self.network = network
    self.upstreams = {}

  def read_docker(self):
    containers = self.docker.containers(filters={'label': LABEL_UPSTREAM_NAME})
    self.upstreams = {}
    for container in containers:
      cid = container['Id']
      try:
        upstream = self.read_container(cid)
      except Exception as e: 
        print(e)
      else:
        self.add_upstream(upstream.name, cid, upstream)
    return self.upstreams
    
  def monitor_docker(self):
    event_filter = {
      'Type': 'continer',
      'status': ['start', 'kill']
    }
    for event in self.docker.events(filters=event_filter, decode=True):
      if LABEL_UPSTREAM_NAME in event['Actor']['Attributes']:
        cid = event['id']
        status = event['status']
        modified = False
        print('container "%s" status chaged to %s' % (event['Actor']['Attributes']['name'], status))
        if status == 'start':
          try:
            upstream = self.read_container(cid)
          except Exception as e: 
            print(e)
          else:
            modified = self.add_upstream(upstream.name, cid, upstream)
        elif status == 'kill':
          name = event['Actor']['Attributes'][LABEL_UPSTREAM_NAME]
          modified = self.remove_upstream(name, cid)
        if modified:
          yield self.upstreams
        
  def add_upstream(self, name, cid, upstream):
    if name in self.upstreams:
      self.upstreams[name][cid] = upstream
    else:
      self.upstreams[name] = {cid: upstream}
    return True
      
  def remove_upstream(self, name, cid):
    if name in self.upstreams and cid in self.upstreams[name]:
      self.upstreams[name].pop(cid)
      return True
    else:
      return False
  
  def read_container(self, cid, limit=3):
    info = self.docker.inspect_container(cid)

    if not self.network.name in info['NetworkSettings']['Networks']:
      if limit == 0:
        raise Exception('invalid network setting for %s', cid[:8])
      self.docker.connect_container_to_network(cid, self.network.id)
      time.sleep(0.5)
      return self.process_container(cid, limit - 1)

    ip = info['NetworkSettings']['Networks'][self.network.name]['IPAddress']

    labels = info['Config']['Labels']
    name = labels[LABEL_UPSTREAM_NAME]

    port = 0
    if LABEL_UPSTREAM_PORT in labels:
      port = int(labels[LABEL_UPSTREAM_PORT])
    else:
      for key in info['NetworkSettings']['Ports'].keys():
        (_port, proto) = key.split('/')
        if proto == 'tcp':
          port = int(_port)
          break

    if port == 0:
      raise Exception('No port found')
    else:
      return Upstream(name, ip, port, 'container id: %s' % cid[:8])


  def reload_loadbalancer(self):
    containers = self.docker.containers(filters={'label': 'org.hamidun.type=loadbalancer'})
    for container in containers:
      executable = self.docker.exec_create(container['Id'], 'nginx -s reload')
      self.docker.exec_start(executable['Id'], True)


class ConfigurationWriter:
  def __init__(self):
    self.upstream_used = set()
    self.upstreams = {}
    self.down_port = NGINX_DEFAULT_DOWN_PORT
    
  def process_template_file(self, in_name, out_name):
    def repl(m):
      name = m.group(1)
      self.upstream_used.add(name)
      return 'upstream_%s' % (name)
    finder = re.compile(r"\{\{upstream\s+(\w+)\}\}")

    with open(in_name, 'r') as fin:
      with open(out_name, 'w') as fout:
        for line in fin:
          fout.write(re.sub(finder, repl, line))

  def write_upstream(self, file_name):
    down_server = '  server localhost:%d; # down' % self.down_port
    with open(file_name, 'w') as fout:
      for name in self.upstream_used:
        fout.write('upstream upstream_%s {\n' % name)
        if name in self.upstreams and len(self.upstreams[name]) > 0:
          servers = self.upstreams[name]
          fout.write('\n'.join([servers[id].to_nginx_server_line() for id in servers]))
        else:
          fout.write(down_server)
        fout.write('\n}\n\n')
      self.write_down_vhost(fout)
      
  def write_down_vhost(self, fout):
    fout.write("""server {
    listen localhost:%d;
    default_type 'text/plain';
    return 503 'Service Unavailable';
}
""" % self.down_port)
      


class MonitorThread(Thread):
  def __init__(self, hamidun, config_writer, upstream_file):
    super().__init__(daemon=True)
    self.hamidun = hamidun
    self.config_writer = config_writer
    self.upstream_file = upstream_file
    self.modified = False
    self.last_modified = 0
    
  def run(self):
    time.sleep(1)
    while True:
      if self.modified:
        now = time.time()
        delta = now - self.last_modified
        if delta >= MODIFIED_MIN_DELTA:
          self.modified = False
          print("writing upstream")
          self.config_writer.write_upstream(self.upstream_file)
          self.hamidun.reload_loadbalancer()
        else:
          time.sleep(MODIFIED_MIN_DELTA - delta)
      else:
        time.sleep(1)

  def notify_changed(self):
    self.last_modified = time.time()
    self.modified = True


class Network:
  def __init__(self, docker, name):
    networks = docker.networks(names=[name])
    if len(networks) > 0:
      self.id = networks[0]['Id']
    else:
      network = docker.create_network(name)
      self.id = network['Id']
    self.name = name

class Upstream:
  def __init__(self, name, address, port, comment):
    self.name = name
    self.nginx_server_line = 'server %s:%d; # %s' % (address, port, comment)
    self.address = address
    self.port = port
    self.comment = comment
    
  def to_nginx_server_line(self):
    return self.nginx_server_line


if __name__ == "__main__":
  main()
