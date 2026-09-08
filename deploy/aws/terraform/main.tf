# GeoRAG on AWS — network, cluster, registry (ADR-0022).
#
# Unlike the Azure deployment, this is written down. There is no Bicep,
# Terraform or ARM template for the Container Apps: ~55 environment
# variables per app were set by hand and drifted freely from
# `.env.production.example`, which `deploy/azure/README.md` records as a
# known gap. Starting from a blank cloud is the one chance to not repeat
# that, so every resource below exists in code or does not exist.
#
# WHAT THIS IS NOT. Two tasks behind an ALB across two AZs is not high
# availability. Every service except laravel-octane runs a single task,
# RDS is Single-AZ, Qdrant and Redis are single tasks holding EFS mounts,
# and the whole platform is stopped nightly on purpose. The second AZ is
# here because an ALB requires two subnets, not because anything fails
# over into it. Read that before sizing anything up.

terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = merge({
      Project   = "georag"
      ManagedBy = "terraform"
    }, var.tags)
  }
}

locals {
  name           = var.name_prefix
  bedrock_region = var.bedrock_region != "" ? var.bedrock_region : var.region

  # Every service, and whether it is reachable from outside the VPC.
  # laravel-octane is the ONLY public one, exactly as on Azure — where it
  # was also the only app with external ingress.
  services = {
    laravel-octane  = { cpu = 1024, memory = 2048, desired = 2, public = true }
    laravel-horizon = { cpu = 1024, memory = 2048, desired = 1, public = false }
    laravel-reverb  = { cpu = 512, memory = 1024, desired = 1, public = false }
    fastapi         = { cpu = 2048, memory = 4096, desired = 1, public = false }
    hatchet         = { cpu = 1024, memory = 2048, desired = 1, public = false }
    # 4 vCPU / 8 GiB, desired 1. Several workflows are max_runs=1
    # singletons and Ch 07 records maxReplicas 1 as a still-open finding,
    # not a free knob. Do not raise this without reading it.
    hatchet-worker = { cpu = 4096, memory = 8192, desired = 1, public = false }
    qdrant         = { cpu = 1024, memory = 4096, desired = 1, public = false }
    redis          = { cpu = 512, memory = 1024, desired = 1, public = false }
    martin         = { cpu = 512, memory = 1024, desired = 1, public = false }
    # SPLADE++ — the one model with no managed equivalent anywhere,
    # including on Cohere (ADR-0022 decision 4). ~440 MB, CPU only. Without
    # it the sparse leg of hybrid retrieval does not exist.
    sparse = { cpu = 512, memory = 2048, desired = 1, public = false }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count                   = var.az_count
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = var.az_count
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 100)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "${local.name}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One NAT gateway, not one per AZ. Tasks need egress for ECR pulls and
# Bedrock; a second NAT would double a fixed monthly cost to protect
# against an AZ failure that nothing else in this deployment survives
# anyway. Revisit together with RDS Multi-AZ, not before.
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "this" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.this]
  tags          = { Name = local.name }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this.id
  }
  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# S3 goes over a gateway endpoint rather than the NAT. Bronze traffic is
# the largest data flow in the system — every uploaded PDF, every page
# render — and NAT charges per gigabyte processed. The endpoint is free.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${local.name}-s3" }
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ingress to laravel-octane and laravel-reverb"
  vpc_id      = aws_vpc.this.id

  ingress {
    description      = "HTTPS from the internet"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  ingress {
    description      = "HTTP, redirected to HTTPS by the listener"
    from_port        = 80
    to_port          = 80
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "ECS tasks. Internal traffic is service-to-service by SG."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "From the ALB"
    from_port       = 0
    to_port         = 65535
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  ingress {
    description = "Service to service inside the VPC"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "RDS and EFS. Reachable only from tasks."
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "Postgres from tasks"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  ingress {
    description     = "NFS from tasks (EFS for Qdrant and Redis)"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

# ---------------------------------------------------------------------------
# Cluster, discovery, registry
# ---------------------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = local.name

  setting {
    # The only source of per-service CPU/memory metrics. Azure got
    # Container Apps platform metrics for free and built eight restart
    # alarms on them; this is the equivalent, and it is not free — it is
    # the price of the alarms in alerts.tf having anything to read.
    name  = "containerInsights"
    value = "enhanced"
  }
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name        = "${local.name}.internal"
  description = "Service-to-service names, replacing Container Apps' internal DNS"
  vpc         = aws_vpc.this.id
}

resource "aws_service_discovery_service" "this" {
  for_each = local.services

  name = each.key

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_ecr_repository" "this" {
  for_each = toset(["laravel", "fastapi", "martin"])

  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  for_each   = aws_ecr_repository.this
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 30 images; CD tags by short SHA"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}
