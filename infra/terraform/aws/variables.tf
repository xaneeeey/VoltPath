variable "region" {
  type    = string
  default = "ap-south-1" # Mumbai: closest region to the Bengaluru fleet
}
variable "env" {
  type    = string
  default = "prod"
}
variable "cluster_version" {
  type    = string
  default = "1.31"
}
variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}
variable "kafka_broker_type" {
  type    = string
  default = "kafka.m7g.large"
}
variable "kafka_brokers" {
  type    = number
  default = 3 # one per AZ
}
variable "redis_node_type" {
  type    = string
  default = "cache.r7g.large"
}
variable "app_node_type" {
  type    = string
  default = "m7g.2xlarge"
}
variable "db_node_type" {
  type    = string
  default = "r7g.2xlarge" # CloudNativePG TimescaleDB pods (memory-heavy)
}
