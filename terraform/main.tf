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

# --- Regional poller providers ---------------------------------------------- #
# S3 landing + the Fargate rollup stay centralized on the default (us-east-1)
# provider above -- only the poller boxes are regional (box_eu.tf/box_au.tf).
# default_tags is NOT inherited from the default provider block, so it's
# repeated on every alias.

provider "aws" {
  alias   = "eu"
  region  = var.eu_region
  profile = var.aws_profile

  default_tags {
    tags = {
      project   = "rail-archiver"
      managedby = "terraform"
    }
  }
}

provider "aws" {
  alias   = "au"
  region  = var.au_region
  profile = var.aws_profile

  default_tags {
    tags = {
      project   = "rail-archiver"
      managedby = "terraform"
    }
  }
}
