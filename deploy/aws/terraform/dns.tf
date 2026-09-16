# ---------------------------------------------------------------------------
# TLS certificate and DNS
# ---------------------------------------------------------------------------
# Until this file existed, `acm_certificate_arn` was a required input and
# nothing in the tree pointed the domain at the load balancer. That left two
# steps that could only be done by hand, in the console, on the one path every
# deployment has to walk:
#
#   * issue a certificate and prove domain ownership, and
#   * create the record aliasing app_domain at the ALB -- whose DNS name does
#     not exist until after the first apply, so it could not even be written
#     down in advance.
#
# The second is the one that mattered. A record created by hand is invisible
# to the state file: it survives a `terraform destroy`, points at a load
# balancer that no longer exists, and the next apply neither adopts nor
# corrects it. That is the Azure failure this tree was written to avoid --
# deploy/aws/README.md opens on it -- reproduced in the DNS layer.
#
# The whole chain is declarative now: register the domain, set `app_domain`,
# apply.

variable "manage_dns" {
  description = <<-EOT
    Whether Terraform issues the certificate and manages DNS in Route 53.

    true (default) needs a Route 53 public hosted zone for the domain, which
    registering through Route 53 creates for you. Terraform then issues the
    ACM certificate, writes its own validation records, waits for issuance,
    and aliases `app_domain` at the ALB.

    false restores the previous behaviour exactly: nothing here is created and
    `acm_certificate_arn` goes back to being a required input. That is the
    setting for a domain hosted somewhere Terraform cannot write records --
    Cloudflare, Namecheap -- where the validation CNAMEs and the record
    pointing at the ALB are pasted in by hand at the registrar.
  EOT
  type        = bool
  default     = true
}

variable "hosted_zone_name" {
  description = <<-EOT
    The Route 53 public hosted zone that contains `app_domain`. Empty means
    the zone IS `app_domain`, which is the apex case and the common one.

    Set it only when deploying to a subdomain of a zone you already own:
    app_domain = "georag.example.com" with hosted_zone_name = "example.com".
    It cannot be derived by chopping a label off `app_domain`, because where
    the zone cut falls is not a function of the string -- "example.co.uk" is
    a registrable domain and "co.uk" is not.
  EOT
  type        = string
  default     = ""
}

locals {
  # `edge = "cloudfront"` means there is no domain and no certificate to issue:
  # the distribution serves *.cloudfront.net on AWS's own certificate. Every
  # resource in this file is skipped, and app_domain goes unused.
  dns             = var.manage_dns && var.edge == "alb" ? 1 : 0
  issue_cert      = var.manage_dns && var.edge == "alb" && var.acm_certificate_arn == "" ? 1 : 0
  zone_name       = var.hosted_zone_name != "" ? var.hosted_zone_name : var.app_domain
  certificate_arn = var.acm_certificate_arn != "" ? var.acm_certificate_arn : one(aws_acm_certificate_validation.this[*].certificate_arn)
}

# Looked up, never created. Registering a domain through Route 53 creates a
# public hosted zone for it automatically, and that zone's four nameservers
# are the ones written into the registration. A second zone for the same name
# is legal, gets DIFFERENT nameservers, and resolves for nobody -- while
# Terraform reports success and the records look correct in the console.
data "aws_route53_zone" "this" {
  count = local.dns

  name         = local.zone_name
  private_zone = false
}

resource "aws_acm_certificate" "this" {
  count = local.issue_cert

  domain_name       = var.app_domain
  validation_method = "DNS"

  # ACM certificates are regional and must live in the SAME region as the load
  # balancer that serves them. There is no default provider alias here because
  # the ALB is in `region` too; a CloudFront distribution would have needed
  # us-east-1 specifically, and does not exist in this architecture.

  lifecycle {
    create_before_destroy = true
  }

  tags = var.tags
}

# DNS validation, rather than email: it needs no mailbox at the domain and it
# renews without a human. ACM re-validates automatically for as long as these
# records stay in place, which is why they are managed here and not deleted
# after the first issuance.
resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in flatten(aws_acm_certificate.this[*].domain_validation_options) :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = data.aws_route53_zone.this[0].zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

# Blocks the apply until ACM reports the certificate ISSUED. Without this the
# HTTPS listener can be handed a PENDING_VALIDATION certificate, which fails
# the attach and leaves a half-built ALB behind.
resource "aws_acm_certificate_validation" "this" {
  count = local.issue_cert

  certificate_arn         = aws_acm_certificate.this[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# Gated on the power switch, unlike the certificate above: this aliases the
# ALB, and power=off destroys the ALB. A record left pointing at a deleted
# load balancer is a dangling alias.
#
# The certificate deliberately survives a power cycle -- public ACM
# certificates are free, and re-validating one on every power-on would add
# minutes to a fifteen-minute restart for no benefit.
resource "aws_route53_record" "app" {
  count = local.dns * local.on

  zone_id = data.aws_route53_zone.this[0].zone_id
  name    = var.app_domain
  type    = "A"

  # An alias, not a CNAME. The ALB's address changes without notice, and at a
  # zone apex -- the common case here -- a CNAME is illegal per RFC 1034.
  alias {
    name                   = aws_lb.this[0].dns_name
    zone_id                = aws_lb.this[0].zone_id
    evaluate_target_health = true
  }
}

# Only meaningful on the `edge = "alb"` path, which is the only path where
# app_domain is set. On the default `edge = "cloudfront"` path app_domain is
# "" by precondition, and interpolating it produced the bare string
# "https://" — an output an operator would reasonably paste into a browser.
# outputs.tf's `public_url` is the one that is correct in both modes; this
# now says so rather than quietly returning a scheme with no host.
output "app_url" {
  description = "The URL the application is served on, once DNS propagates. Empty unless edge = \"alb\" — use `public_url` for the mode-independent answer."
  value       = var.app_domain == "" ? "" : "https://${var.app_domain}"
}

