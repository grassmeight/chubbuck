# OAC = Origin Access Control, the modern replacement for OAI. Lets
# CloudFront sign requests to S3 with sigv4 so the bucket can stay private.
resource "aws_cloudfront_origin_access_control" "s3_static" {
  name                              = "${var.project_name}-s3-static-oac"
  description                       = "OAC for the ${var.project_name} static UI bucket"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Function URL comes back as "https://abc123.lambda-url.us-east-1.on.aws/".
# CloudFront's origin domain_name needs the bare hostname.
locals {
  function_url_host = replace(
    replace(aws_lambda_function_url.app.function_url, "https://", ""),
    "/", ""
  )
}

resource "aws_cloudfront_distribution" "main" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  comment             = "${var.project_name} — static UI + Lambda /api proxy"
  price_class         = var.cloudfront_price_class
  http_version        = "http2and3"

  # ---------------------------------------------------------------------------
  # Origin 1: S3 static bucket (index.html and assets)
  # ---------------------------------------------------------------------------
  origin {
    origin_id                = "s3-static"
    domain_name              = aws_s3_bucket.static.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3_static.id
  }

  # ---------------------------------------------------------------------------
  # Origin 2: Lambda Function URL (the /api/* proxy target)
  # ---------------------------------------------------------------------------
  origin {
    origin_id   = "lambda-fn-url"
    domain_name = local.function_url_host

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # ---------------------------------------------------------------------------
  # Default behavior — S3 static, aggressive caching.
  # ---------------------------------------------------------------------------
  default_cache_behavior {
    target_origin_id       = "s3-static"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # AWS managed: CachingOptimized (1d default TTL, gzip/br on, query strings ignored).
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # ---------------------------------------------------------------------------
  # /api/* — Lambda, no caching, forward everything except Host header.
  # ---------------------------------------------------------------------------
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    target_origin_id       = "lambda-fn-url"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true

    # AWS managed: CachingDisabled.
    cache_policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"

    # AWS managed: AllViewerExceptHostHeader. Forwards all viewer headers,
    # cookies, and query strings to the origin EXCEPT Host (Lambda Function
    # URLs require their own Host to validate the request).
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Default *.cloudfront.net certificate. Swap to ACM-issued cert (in
  # us-east-1) when you wire up a custom domain.
  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
