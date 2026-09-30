# VoltPath on AWS (Terraform)

Provisions:
- a VPC (3 AZs, flow logs)
- EKS with an app node group and a tainted db node group for CloudNativePG + TimescaleDB
- MSK (Kafka, TLS + IAM auth, RF=3)
- ElastiCache Redis (multi-AZ, TLS)
- S3 lake with KMS encryption and lifecycle tiering
- Secrets Manager
- an IRSA role

Everything at rest is encrypted with a rotating KMS key (AES-256).

```bash
tofu init && tofu plan -var env=prod     # or terraform
helm install cnpg cnpg/cloudnative-pg      # then apply infra/k8s/cnpg-timescale.yaml
helm install voltpath ../../helm/voltpath -n voltpath --create-namespace \
  --set endpoints.kafkaBootstrap=$(tofu output -raw kafka_bootstrap_brokers_iam)
```

**Other clouds:** the Helm chart is cloud-neutral. Swap this module for GKE + Confluent/Managed Kafka + Memorystore + GCS, or for AKS + Event Hubs (Kafka API) + Azure Cache + Blob. The application only reads endpoints from values and secrets.
