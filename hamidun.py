#!/usr/bin/env python

from docker import Client
from os import listdir
from os.path import isfile, join
import re
import ConfigParser

def main():
  config = ConfigParser.ConfigParser()
  config.read('/etc/hamidun.conf')
  
  docker_host = config.get('Docker', 'host') or 'unix://var/run/docker.sock'
  docker_network_name = config.get('Docker', 'shared_network') or 'load_balancer'

  template_dir = config.get('NginX', 'templates') or '/etc/nginx/sites-template'
  out_dir = config.get('NginX', 'output') or '/etc/nginx/sites-enabled'

  upstreams = load_configuration(docker_host, docker_network_name)
  files = [f for f in listdir(template_dir) if isfile(join(template_dir, f))]
  for f in files:
    process_file(join(template_dir, f), join(out_dir, f), upstreams)
  # finish

def load_configuration(host, network_name):
  result = {}
  client = Client(base_url=host)
  containers = client.containers(filters={'label': 'org.hamidun.name'})
  for container in containers:
    cid = container['Id']
    info = client.inspect_container(cid)

    if not network_name in info['NetworkSettings']['Networks']:
      print('invalid network setting for %s' % cid[:8])
      continue

    network = info['NetworkSettings']['Networks'][network_name]
    ip = network['IPAddress']

    labels = info['Config']['Labels']
    name = labels['org.hamidun.name']

    port = 0
    if 'org.hamidun.port' in labels:
      port = int(labels['org.hamidun.port'])
    else:
      for key in info['NetworkSettings']['Ports'].keys():
        (_port, proto) = key.split('/')
        if proto == 'tcp':
          port = int(_port)
          break

    if port == 0:
      print('WARNING: container %s have label %s but no opened port found' % (cid, label))
    else:
      upstream = {
      'container': cid,
      'address': ip,
      'port': port
      }
      if name in result:
        result[name].append(upstream)
      else:
        result[name] = [upstream]
  return result


def process_file(in_name, out_name, upstreams):
  def repl(m):
    name = m.group(1)
    servers = []
    if name in upstreams:
      servers = upstreams[name]
    else:
      servers = [{'address': 'localhost', 'port': 9080, 'container': 'down'}]
    return create_nginx_upstream(name, servers)
  finder = re.compile(r"\{\{upstream\s+(\w+)\}\}")

  with open(in_name, 'r') as fin:
    with open(out_name, 'w') as fout:
      for line in fin:
        fout.write(re.sub(finder, repl, line))


def create_nginx_upstream(name, servers):
  builder = ['upstream %s {\n' % name]
  builder.extend([format_server_line(server) for server in servers])
  builder.append('}')
  return ''.join(builder)


def format_server_line(data):
  return '  server %s:%d; # container id: %s\n' % (data['address'], data['port'], data['container'][:8])



main()
