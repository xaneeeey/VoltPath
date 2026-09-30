output "cluster_name" { value = module.eks.cluster_name }
output "kafka_bootstrap_brokers_iam" { value = aws_msk_cluster.this.bootstrap_brokers_sasl_iam }
output "redis_primary_endpoint" { value = aws_elasticache_replication_group.this.primary_endpoint_address }
output "lake_bucket" { value = aws_s3_bucket.lake.bucket }
output "app_secret_arn" { value = aws_secretsmanager_secret.app.arn }
output "lake_role_arn" { value = module.lake_irsa.iam_role_arn }
