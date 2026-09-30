# VoltPath on AWS. Stateless services run on EKS (Helm chart in infra/helm/voltpath); the event log,
# cache and lake use managed services; PostgreSQL+TimescaleDB+pgvector runs in-cluster via the
# CloudNativePG operator because RDS does not offer the timescaledb extension - and the same
# operator manifests run unchanged on GKE/AKS, keeping the platform cloud-agnostic.

data "aws_availability_zones" "available" { state = "available" }
locals {
  name = "voltpath-${var.env}"
  azs  = slice(data.aws_availability_zones.available.names, 0, 3)
}

# ---------------------------------------------------------------- encryption (AES-256 at rest)
resource "aws_kms_key" "data" {
  description             = "${local.name} data-at-rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}
resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

# ---------------------------------------------------------------- network
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name                      = local.name
  cidr                      = var.vpc_cidr
  azs                       = local.azs
  private_subnets           = [for i in range(3) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets            = [for i in range(3) : cidrsubnet(var.vpc_cidr, 8, 48 + i)]
  enable_nat_gateway        = true
  one_nat_gateway_per_az    = true
  enable_flow_log           = true
  flow_log_destination_type = "s3"
  flow_log_destination_arn  = aws_s3_bucket.logs.arn
  private_subnet_tags       = { "kubernetes.io/role/internal-elb" = 1 }
  public_subnet_tags        = { "kubernetes.io/role/elb" = 1 }
}

# ---------------------------------------------------------------- Kubernetes
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name                   = local.name
  cluster_version                = var.cluster_version
  vpc_id                         = module.vpc.vpc_id
  subnet_ids                     = module.vpc.private_subnets
  cluster_endpoint_public_access = false
  enable_irsa                    = true
  cluster_encryption_config      = { resources = ["secrets"], provider_key_arn = aws_kms_key.data.arn }
  cluster_enabled_log_types      = ["api", "audit", "authenticator"]

  eks_managed_node_groups = {
    app = {
      instance_types = [var.app_node_type]
      ami_type       = "AL2023_ARM_64_STANDARD"
      min_size       = 3
      max_size       = 12
      desired_size   = 4
      labels         = { workload = "app" }
    }
    db = {
      instance_types = [var.db_node_type]
      ami_type       = "AL2023_ARM_64_STANDARD"
      min_size       = 3
      max_size       = 3
      desired_size   = 3
      labels         = { workload = "db" }
      taints         = [{ key = "workload", value = "db", effect = "NO_SCHEDULE" }]
      block_device_mappings = {
        xvda = { device_name = "/dev/xvda", ebs = { volume_size = 100, volume_type = "gp3", encrypted = true, kms_key_id = aws_kms_key.data.arn } }
      }
    }
  }
}

# ---------------------------------------------------------------- event log (Kafka API)
resource "aws_security_group" "data" {
  name   = "${local.name}-data"
  vpc_id = module.vpc.vpc_id
  ingress {
    description     = "Kafka TLS + Redis TLS from EKS nodes only"
    from_port       = 6379
    to_port         = 9098
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_msk_configuration" "this" {
  name              = local.name
  kafka_versions    = ["3.7.x"]
  server_properties = <<-EOT
    auto.create.topics.enable=false
    default.replication.factor=3
    min.insync.replicas=2
    num.partitions=12
    log.retention.hours=24
  EOT
}

resource "aws_msk_cluster" "this" {
  cluster_name           = local.name
  kafka_version          = "3.7.x"
  number_of_broker_nodes = var.kafka_brokers
  broker_node_group_info {
    instance_type   = var.kafka_broker_type
    client_subnets  = module.vpc.private_subnets
    security_groups = [aws_security_group.data.id]
    storage_info {
      ebs_storage_info { volume_size = 1000 }
    }
  }
  configuration_info {
    arn      = aws_msk_configuration.this.arn
    revision = aws_msk_configuration.this.latest_revision
  }
  encryption_info {
    encryption_at_rest_kms_key_arn = aws_kms_key.data.arn
    encryption_in_transit {
      client_broker = "TLS"
      in_cluster    = true
    }
  }
  client_authentication {
    sasl { iam = true }
  }
  logging_info {
    broker_logs {
      cloudwatch_logs {
        enabled   = true
        log_group = aws_cloudwatch_log_group.msk.name
      }
    }
  }
}
resource "aws_cloudwatch_log_group" "msk" {
  name              = "/voltpath/${var.env}/msk"
  retention_in_days = 30
  kms_key_id        = aws_kms_key.data.arn
}

# ---------------------------------------------------------------- hot state (Redis)
resource "aws_elasticache_subnet_group" "this" {
  name       = local.name
  subnet_ids = module.vpc.private_subnets
}
resource "aws_elasticache_replication_group" "this" {
  replication_group_id       = local.name
  description                = "VoltPath hot vehicle state, GEO index, rate limits"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2 # primary + replica, automatic failover
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.data.arn
  transit_encryption_enabled = true
  subnet_group_name          = aws_elasticache_subnet_group.this.name
  security_group_ids         = [aws_security_group.data.id]
  parameter_group_name       = "default.redis7"
}

# ---------------------------------------------------------------- lake (S3, tiered)
resource "aws_s3_bucket" "lake" { bucket = "${local.name}-lake" }
resource "aws_s3_bucket" "logs" { bucket = "${local.name}-logs" }

resource "aws_s3_bucket_server_side_encryption_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}
resource "aws_s3_bucket_public_access_block" "lake" {
  bucket                  = aws_s3_bucket.lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "lake" {
  bucket = aws_s3_bucket.lake.id
  versioning_configuration { status = "Enabled" }
}
# hot/warm/cold: raw telemetry -> Standard-IA after 30 d -> Glacier Instant Retrieval after 90 d -> delete after 3 y
resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "telemetry-tiering"
    status = "Enabled"
    filter { prefix = "telemetry/" }
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration { days = 1095 }
  }
}

# ---------------------------------------------------------------- secrets + workload identity
resource "aws_secretsmanager_secret" "app" {
  name       = "voltpath/${var.env}"
  kms_key_id = aws_kms_key.data.arn
}

module "lake_irsa" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name = "${local.name}-lake"
  oidc_providers = {
    main = { provider_arn = module.eks.oidc_provider_arn, namespace_service_accounts = ["voltpath:voltpath"] }
  }
  role_policy_arns = { lake = aws_iam_policy.lake.arn }
}
resource "aws_iam_policy" "lake" {
  name = "${local.name}-lake"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"], Resource = [aws_s3_bucket.lake.arn, "${aws_s3_bucket.lake.arn}/*"] },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = [aws_kms_key.data.arn] },
      { Effect = "Allow", Action = ["kafka-cluster:Connect", "kafka-cluster:*Topic*", "kafka-cluster:ReadData", "kafka-cluster:WriteData", "kafka-cluster:*Group*"], Resource = ["*"] },
    ]
  })
}
