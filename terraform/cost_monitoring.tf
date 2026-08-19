# --- Cost monitoring: budget + anomaly alerts scoped to this project ------ #
# Every resource here is created via the provider's default_tags (main.tf),
# so it's already tagged project=rail-archiver. This file activates that tag
# as a cost allocation tag and filters Budgets/Anomaly Detection by it, so
# alerts reflect this project's spend rather than the whole (shared) account.
#
# Known gap: the second EC2 box (dashboard-api, shared with the unrelated
# gtfs-rt-rater project) is not managed by this terraform and isn't tagged,
# so its cost won't show up under the project filter below.

resource "aws_ce_cost_allocation_tag" "project" {
  tag_key = "project"
  status  = "Active"
}

resource "aws_sns_topic" "cost_alerts" {
  name = "rail-archiver-cost-alerts"
}

resource "aws_sns_topic_subscription" "cost_alerts_email" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "email"
  endpoint  = var.cost_alert_email
}

# AWS Budgets and Cost Anomaly Detection each need explicit permission to
# publish to the topic — they don't inherit the caller's IAM permissions.
resource "aws_sns_topic_policy" "cost_alerts" {
  arn = aws_sns_topic.cost_alerts.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowBudgetsPublish"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.cost_alerts.arn
        Condition = {
          StringEquals = { "AWS:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      },
      {
        Sid       = "AllowCostAnomalyPublish"
        Effect    = "Allow"
        Principal = { Service = "costalerts.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.cost_alerts.arn
        Condition = {
          StringEquals = { "AWS:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      }
    ]
  })
}

# Monthly cost budget, filtered to this project's tagged resources. Amount is
# a placeholder — the account is shared with other projects, so there's no
# real baseline for just this project's tagged spend yet. Revisit after the
# cost allocation tag has a month of data (activation can take up to 24h to
# start populating, and this is the first month it's ever been tracked).
resource "aws_budgets_budget" "project" {
  name         = "rail-archiver-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_cost_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:project$rail-archiver"]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alerts.arn]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alerts.arn]
  }

  depends_on = [aws_sns_topic_policy.cost_alerts]
}

# Cost Anomaly Detection catches spend spikes (e.g. the 913GiB landing-bucket
# growth incident) automatically instead of waiting on a manual audit.
resource "aws_ce_anomaly_monitor" "project" {
  name         = "rail-archiver-cost-anomaly"
  monitor_type = "CUSTOM"
  monitor_specification = jsonencode({
    Tags = {
      Key          = "project"
      Values       = ["rail-archiver"]
      MatchOptions = ["EQUALS"]
    }
  })
}

resource "aws_ce_anomaly_subscription" "project" {
  name             = "rail-archiver-cost-anomaly-subscription"
  frequency        = "DAILY"
  monitor_arn_list = [aws_ce_anomaly_monitor.project.arn]

  subscriber {
    type    = "SNS"
    address = aws_sns_topic.cost_alerts.arn
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      values        = [tostring(var.cost_anomaly_threshold_usd)]
      match_options = ["GREATER_THAN_OR_EQUAL"]
    }
  }

  depends_on = [aws_sns_topic_policy.cost_alerts]
}
