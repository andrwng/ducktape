# Copyright 2014 Confluent Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .service import Service
import time, re
from ducttape.services.schema_registry_utils import SCHEMA_REGISTRY_DEFAULT_REQUEST_PROPERTIES
from ducttape.services.kafka_rest_utils import KAFKA_REST_DEFAULT_REQUEST_PROPERTIES



class ZookeeperService(Service):
    def __init__(self, cluster, num_nodes):
        super(ZookeeperService, self).__init__(cluster, num_nodes)

    def start(self):
        super(ZookeeperService, self).start()
        config = """
dataDir=/mnt/zookeeper
clientPort=2181
maxClientCnxns=0
initLimit=5
syncLimit=2
quorumListenOnAllIPs=true
"""
        for idx, node in enumerate(self.nodes, 1):
            template_params = { 'idx': idx, 'host': node.account.hostname }
            config += "server.%(idx)d=%(host)s:2888:3888\n" % template_params

        for idx, node in enumerate(self.nodes, 1):
            self.logger.info("Starting ZK node %d on %s", idx, node.account.hostname)
            self._stop_and_clean(node, allow_fail=True)
            node.account.ssh("mkdir -p /mnt/zookeeper")
            node.account.ssh("echo %d > /mnt/zookeeper/myid" % idx)
            node.account.create_file("/mnt/zookeeper.properties", config)
            node.account.ssh("/opt/kafka/bin/zookeeper-server-start.sh /mnt/zookeeper.properties 1>> /mnt/zk.log 2>> /mnt/zk.log &")
            time.sleep(5) # give it some time to start

    def stop(self):
        """If the service left any running processes or data, clean them up."""
        for idx, node in enumerate(self.nodes, 1):
            self.logger.info("Stopping %s node %d on %s" % (type(self).__name__, idx, node.account.hostname))
            self._stop_and_clean(node)
            node.free()

    def _stop_and_clean(self, node, allow_fail=False):
        # This uses Kafka-REST's stop service script because it's better behaved
        # (knows how to wait) and sends SIGTERM instead of
        # zookeeper-stop-server.sh's SIGINT. We don't actually care about clean
        # shutdown here, so it's ok to use the bigger hammer
        node.account.ssh("/opt/kafka-rest/bin/kafka-rest-stop-service zookeeper", allow_fail=allow_fail)
        node.account.ssh("rm -rf /mnt/zookeeper /mnt/zookeeper.properties /mnt/zk.log")

    def connect_setting(self):
        return ','.join([node.account.hostname + ':2181' for node in self.nodes])


class KafkaService(Service):
    def __init__(self, cluster, num_nodes, zk, topics=None):
        super(KafkaService, self).__init__(cluster, num_nodes)
        self.zk = zk
        self.topics = topics

    def start(self):
        super(KafkaService, self).start()
        template = open('templates/kafka.properties').read()
        zk_connect = self.zk.connect_setting()
        for idx,node in enumerate(self.nodes,1):
            self.logger.info("Starting Kafka node %d on %s", idx, node.account.hostname)
            self._stop_and_clean(node, allow_fail=True)
            template_params = {
                'broker_id': idx,
                'hostname': node.account.hostname,
                'zk_connect': zk_connect
            }
            config = template % template_params
            node.account.create_file("/mnt/kafka.properties", config)
            node.account.ssh("/opt/kafka/bin/kafka-server-start.sh /mnt/kafka.properties 1>> /mnt/kafka.log 2>> /mnt/kafka.log &")
            time.sleep(5)  # wait for start up

        if self.topics is not None:
            node = self.nodes[0]  # any node is fine here
            for topic,settings in self.topics.items():
                if settings is None:
                    settings = {}
                self.logger.info("Creating topic %s with settings %s", topic, settings)
                node.account.ssh(
                    "/opt/kafka/bin/kafka-topics.sh --zookeeper %(zk_connect)s --create "\
                    "--topic %(name)s --partitions %(partitions)d --replication-factor %(replication)d" % {
                        'zk_connect': zk_connect,
                        'name': topic,
                        'partitions': settings.get('partitions', 1),
                        'replication': settings.get('replication-factor', 1)
                    })

    def stop(self):
        """If the service left any running processes or data, clean them up."""
        for idx, node in enumerate(self.nodes, 1):
            self.logger.info("Stopping %s node %d on %s" % (type(self).__name__, idx, node.account.hostname))
            self._stop_and_clean(node)
            node.free()

    def _stop_and_clean(self, node, allow_fail=False):
        node.account.ssh("/opt/kafka/bin/kafka-server-stop.sh", allow_fail=allow_fail)
        time.sleep(5)  # the stop script doesn't wait
        node.account.ssh("rm -rf /mnt/kafka-logs /mnt/kafka.properties /mnt/kafka.log")

    def bootstrap_servers(self):
        return ','.join([node.account.hostname + ":9092" for node in self.nodes])


class KafkaRestService(Service):
    def __init__(self, cluster, num_nodes, zk, kafka):
        super(KafkaRestService, self).__init__(cluster, num_nodes)
        self.zk = zk
        self.kafka = kafka
        self.port = 8080

    def start(self):
        super(KafkaRestService, self).start()
        template = open('templates/rest.properties').read()
        zk_connect = self.zk.connect_setting()
        bootstrapServers = self.kafka.bootstrap_servers()
        for idx, node in enumerate(self.nodes, 1):
            self.logger.info("Starting REST node %d on %s", idx, node.account.hostname)
            self._stop_and_clean(node, allow_fail=True)
            template_params = {
                'id': idx,
                'port': self.port,
                'zk_connect': zk_connect,
                'bootstrap_servers': bootstrapServers
            }
            config = template % template_params
            node.account.create_file("/mnt/rest.properties", config)
            node.account.ssh("/opt/kafka-rest/bin/kafka-rest-start /mnt/rest.properties 1>> /mnt/rest.log 2>> /mnt/rest.log &")

            node.account.wait_for_http_service(self.port, headers=KAFKA_REST_DEFAULT_REQUEST_PROPERTIES)

    def stop(self):
        for idx, node in enumerate(self.nodes,1):
            self.logger.info("Stopping REST node %d on %s", idx, node.account.hostname)
            self._stop_and_clean(node)
            node.free()

    def _stop_and_clean(self, node, allow_fail=False):
        node.account.ssh("/opt/kafka-rest/bin/kafka-rest-stop", allow_fail=allow_fail)
        node.account.ssh("rm -rf /mnt/rest.properties /mnt/rest.log")

    def url(self, idx=1):
        return "http://" + self.get_node(idx).account.hostname + ":" + str(self.port)


class SchemaRegistryService(Service):
    def __init__(self, cluster, num_nodes, zk, kafka):
        super(SchemaRegistryService, self).__init__(cluster, num_nodes)
        self.zk = zk
        self.kafka = kafka
        self.port = 8080

    def start(self):
        super(SchemaRegistryService, self).start()

        template = open('templates/schema-registry.properties').read()
        template_params = {
            'kafkastore_topic': '_schemas',
            'kafkastore_url': self.zk.connect_setting(),
            'rest_port': self.port
        }
        config = template % template_params

        for idx, node in enumerate(self.nodes, 1):
            self.logger.info("Starting Schema Registry node %d on %s", idx, node.account.hostname)
            self._stop_and_clean(node, allow_fail=True)
            self.start_node(node, config)

            # Wait for the server to become live
            # TODO - add KafkaRest headers
            node.account.wait_for_http_service(self.port, headers=SCHEMA_REGISTRY_DEFAULT_REQUEST_PROPERTIES)

    def stop(self):
        """If the service left any running processes or data, clean them up."""
        for idx, node in enumerate(self.nodes, 1):
            self.logger.info("Stopping %s node %d on %s" % (type(self).__name__, idx, node.account.hostname))
            self._stop_and_clean(node, True)
            node.free()

    def _stop_and_clean(self, node, allow_fail=False):
        node.account.ssh("/opt/schema-registry/bin/schema-registry-stop", allow_fail=allow_fail)
        node.account.ssh("rm -rf /mnt/schema-registry.properties /mnt/schema-registry.log")

    def stop_node(self, node, clean_shutdown=True, allow_fail=True):
        node.account.kill_process("schema-registry", clean_shutdown, allow_fail)

    def start_node(self, node, config=None):
        if config is None:
            template = open('templates/schema-registry.properties').read()
            template_params = {
                'kafkastore_topic': '_schemas',
                'kafkastore_url': self.zk.connect_setting(),
                'rest_port': self.port
            }
            config = template % template_params

        node.account.create_file("/mnt/schema-registry.properties", config)
        cmd = "/opt/schema-registry/bin/schema-registry-start /mnt/schema-registry.properties " \
            + "1>> /mnt/schema-registry.log 2>> /mnt/schema-registry.log &"

        node.account.ssh(cmd)

    def restart_node(self, node, wait_sec=0, clean_shutdown=True):
        self.stop_node(node, clean_shutdown, allow_fail=True)
        time.sleep(wait_sec)
        self.start_node(node)

    def get_master_node(self):
        node = self.nodes[0]

        cmd = "/opt/kafka/bin/kafka-run-class.sh kafka.tools.ZooKeeperMainWrapper -server %s get /schema-registry-master" \
              % self.zk.connect_setting()

        host = None
        port_str = None
        self.logger.debug("Querying zookeeper to find current schema registry master: \n%s" % cmd)
        for line in node.account.ssh_capture(cmd):
            match = re.match("^{\"host\":\"(.*)\",\"port\":(\d+),", line)
            if match is not None:
                groups = match.groups()
                host = groups[0]
                port_str = groups[1]
                break

        if host is None:
            raise Exception("Could not find schema registry master.")

        base_url = "%s:%s" % (host, port_str)
        self.logger.debug("schema registry master is %s" % base_url)

        # Return the node with this base_url
        for idx, node in enumerate(self.nodes, 1):
            if self.url(idx).find(base_url) >= 0:
                return self.get_node(idx)

    def url(self, idx=1):
        return "http://" + self.get_node(idx).account.hostname + ":" + str(self.port)


class HadoopService(Service):
    def __init__(self, cluster, num_nodes):
        super(HadoopService, self).__init__(cluster, num_nodes)

    def start(self):
        super(HadoopService, self).start()
        
        hadoop_env_template = open('templates/hadoop-env.sh').read()

        hadoop_env_params = {'java_home': ''}

        hdfs_site_template = open('templates/hdfs-site.xml').read()
        hdfs_site_params = {
            'dfs_replication': 1,
            'dfs_name_dir': '/mnt/name',
            'dfs_data_dir': '/mnt/data'
        }

        hdfs_site = hdfs_site_template % hdfs_site_params
        
        core_site_template = open('templates/core-site.xml').read()

        core_site_params = {'fs_default_name': ''}
        
        master_host = None
        
        for idx, node in enumerate(self.nodes, 1):
            self._stop_and_clean(node, allow_fail=True)
            self.logger.info("Stopping HDFS on node %d", idx)
            
            self.logger.info("creating hdfs directories")
            node.account.ssh("mkdir -p /mnt/data")
            node.account.ssh("mkdir -p /mnt/name")
            
            if idx == 1:
                master_host = node.account.hostname
            
            # for line in node.account.ssh_capture("echo $JAVA_HOME"):
            #    self.logger.info(line.strip())

            # node.account.ssh_output("echo $JAVA_HOME")
            
            hadoop_env_params['java_home'] = '/usr/lib/jvm/java-6-oracle'
            hadoop_env = hadoop_env_template % hadoop_env_params
            
            core_site_params['fs_default_name'] = "hdfs://" + master_host + ":9000"   
            core_site = core_site_template % core_site_params

            node.account.create_file("/mnt/hadoop-env.sh", hadoop_env)
            node.account.create_file("/mnt/core-site.xml", core_site)
            node.account.create_file("/mnt/hdfs-site.xml", hdfs_site)

            if idx == 1:
                node.account.ssh("HADOOP_CONF_DIR=/mnt /opt/hadoop-cdh/bin/hadoop namenode -format")
                time.sleep(1)
                self.start_namenode(node)
            else:
                self.start_datanode(node)
            time.sleep(5)  # wait for start up
    
    def start_namenode(self, node):
        node.account.ssh("/opt/hadoop-cdh/sbin/hadoop-daemon.sh --config /mnt start namenode &")

    def start_datanode(self, node):
        node.account.ssh("/opt/hadoop-cdh/sbin/hadoop-daemon.sh --config /mnt start datanode &")

    def stop(self):
        for idx, node in enumerate(self.nodes, 1):
            self._stop_and_clean(node)
            node.free()

    def _stop_and_clean(self, node, allow_fail=False):
        node.account.ssh("pkill -f \'java\'", True)
        node.account.ssh("rm -rf /mnt/*")
        node.account.ssh("/opt/hadoop-cdh/sbin/hadoop-daemon.sh --config /mnt stop datanode &")
        node.account.ssh("/opt/hadoop-cdh/sbin/hadoop-daemon.sh --config /mnt stop namenode &")
        time.sleep(5)  # the stop script doesn't wait
        node.account.ssh("rm -rf /mnt/core-site.xml /mnt/hdfs-site.xml /mnt/hadoop-env.sh")


class HadoopV1Service(HadoopService):
    def __init__(self, cluster, num_nodes):
        super(HadoopV1Service, self).__init__(cluster, num_nodes)

    def start(self):
        super(HadoopV1Service, self).start()

        mapred_site_template = open('templates/mapred-site.xml').read()

        mapred_site_params = {'mapred_job_tracker': ''}
        
        master_host = None
        
        for idx, node in enumerate(self.nodes, 1):
            self._stop_and_clean(node, allow_fail=True)
            self.logger.info("clean up finished on node %d", idx)
            if idx == 1:
                master_host = node.account.hostname
            
            node.account.ssh("cp /opt/hadoop-cdh/etc/hadoop-mapreduce1/hadoop-metrics.properties /mnt")
            
            mapred_site_params['mapred_job_tracker'] = master_host + ":54311"
            mapred_site = mapred_site_template % mapred_site_params
            node.account.create_file("/mnt/mapred-site.xml", mapred_site)

            if idx == 1:
                self.start_jobtracker(node)
            else:
                self.start_tasktracker(node)
            time.sleep(5)

    def start_jobtracker(self, node):
        node.account.ssh("/opt/hadoop-cdh/bin-mapreduce1/hadoop-daemon.sh --config /mnt start jobtracker &")

    def start_tasktracker(self, node):
        node.account.ssh("/opt/hadoop-cdh/bin-mapreduce1/hadoop-daemon.sh --config /mnt start tasktracker &")

    def _stop_and_clean(self, node, allow_fail=False):
        node.account.ssh("/opt/hadoop-cdh/bin-mapreduce1/stop-mapred.sh &", allow_fail=allow_fail)
        time.sleep(5)  # the stop script doesn't wait


class HadoopV2Service(HadoopService):
    def __init__(self, cluster, num_nodes):
        super(HadoopV2Service, self).__init__(cluster, num_nodes)
        self.master_host = None

    def start(self):
        super(HadoopV2Service, self).start()
        
        mapred_site_template = open('templates/mapred2-site.xml').read()

        mapred_site_params = {
            'mapreduce_jobhistory_address': ''
        }

        yarn_env_template = open('templates/hadoop-env.sh').read()

        yarn_env_params = {'java_home': ''}

        yarn_site_template = open('templates/yarn-site.xml').read()
        yarn_site_params = {'yarn_resource_manager_address': ''}
        
        for idx, node in enumerate(self.nodes, 1):
            self._stop_and_clean(node, allow_fail=True)
            self.logger.info("clean up finished on node %d", idx)
            
            if idx == 1:
                self.master_host = node.account.hostname
            
            node.account.ssh("cp /opt/hadoop-cdh/etc/hadoop-mapreduce1/hadoop-metrics.properties /mnt")
            
            yarn_env_params['java_home'] = '/usr/lib/jvm/java-6-oracle'
            yarn_env = yarn_env_template % yarn_env_params

            yarn_site_params['yarn_resourcemanager_hostname'] = self.master_host
            yarn_site = yarn_site_template % yarn_site_params

            mapred_site_params['mapreduce_jobhistory_address'] = self.master_host + ":10020"
            mapred_site = mapred_site_template % mapred_site_params

            node.account.create_file("/mnt/yarn-env.sh", yarn_env)
            node.account.create_file("/mnt/mapred-site.xml", mapred_site)
            node.account.create_file("/mnt/yarn-site.xml", yarn_site)
            
            if idx == 1:
                self.start_resourcemanager(node)
                self.start_jobhistoryserver(node)
            else:
                self.start_nodemanager(node)
            time.sleep(5)
    
    def start_resourcemanager(self, node):
        node.account.ssh("/opt/hadoop-cdh/sbin/yarn-daemon.sh --config /mnt start resourcemanager &")

    def start_nodemanager(self, node): 
        node.account.ssh("/opt/hadoop-cdh/sbin/yarn-daemon.sh --config /mnt start nodemanager &")

    def start_jobhistoryserver(self, node):
        node.account.ssh("/opt/hadoop-cdh/sbin/mr-jobhistory-daemon.sh --config /mnt start historyserver &")

    def _stop_and_clean(self, node, allow_fail=False):
        node.account.ssh(
            "/opt/hadoop-cdh/sbin/yarn-daemon.sh --config /mnt stop nodemanager &", allow_fail=allow_fail)
        node.account.ssh(
            "/opt/hadoop-cdh/sbin/yarn-daemon.sh --config /mnt stop resourcemanager &", allow_fail=allow_fail)
        node.account.ssh("rm -rf /mnt/yarn-site.xml /mnt/mapred-site.xml /mnt/yarn-env.sh")
        # node.account.ssh("pkill -f \'java\'", True)
        # node.account.ssh("rm -rf /mnt/*")
        time.sleep(5)  # the stop script doesn't wait

