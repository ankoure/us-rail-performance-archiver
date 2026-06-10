terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  # State is local for now. If this grows, move to an S3 backend.
}

provider "aws" {
  region = var.region
  # Optional: pin the named profile so creds resolve like the aws CLI does,
  # independent of AWS_PROFILE being exported. Pass -var aws_profile=KourePowerUser.
  profile = var.aws_profile

  default_tags {
    tags = {
      project   = "rail-archiver"
      managedby = "terraform"
    }
  }
}
