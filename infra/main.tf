# aws-job-streamer — infrastructure.
#
# Phase 1 provisions only what the local pipeline actually uses today: the dedup table and the
# job-API secrets. Lambda, EventBridge, Step Functions, SES sending and SNS arrive in Phases 3-4,
# when there is code that needs them. Nothing is declared before it is used.
#
# Credentials come from the EC2 instance role — there are no access keys on this box.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # State is LOCAL for now, and gitignored: it records resource attributes in plaintext.
  # Phase 5 moves it to an S3 backend with DynamoDB locking, once CI needs to run terraform too.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "aws-job-streamer"
      ManagedBy = "terraform"
      Repo      = "github.com/lubobali/aws-job-streamer"
    }
  }
}
