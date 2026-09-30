terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
  # Remote state (create the bucket/table once per account):
  # backend "s3" { bucket = "voltpath-tfstate" key = "prod/terraform.tfstate" region = "ap-south-1" dynamodb_table = "voltpath-tflock" encrypt = true }
}

provider "aws" {
  region = var.region
  default_tags { tags = { project = "voltpath", env = var.env, managed_by = "terraform" } }
}
