output "jobs_table_name" {
  description = "DynamoDB dedup table. The app reads this to know whether a job is new."
  value       = aws_dynamodb_table.jobs.name
}

output "ssm_adzuna_app_id_name" {
  description = "SSM parameter holding the Adzuna app_id."
  value       = aws_ssm_parameter.adzuna_app_id.name
}

output "ssm_adzuna_app_key_name" {
  description = "SSM parameter NAME for the Adzuna app_key. The value is never output."
  value       = aws_ssm_parameter.adzuna_app_key.name
}

output "digest_recipient" {
  description = "Address the digest is sent TO (verified for the SES sandbox)."
  value       = aws_ses_email_identity.digest_recipient.email
}

output "digest_sender" {
  description = "Address the digest is sent FROM. Works once the DKIM records below are live."
  value       = local.sender_address
}

output "dkim_dns_records" {
  description = "The 3 CNAMEs to add at your DNS host. Host/Value pairs, ready to paste."
  value = [
    for token in aws_ses_domain_dkim.sender.dkim_tokens : {
      type  = "CNAME"
      host  = "${token}._domainkey"
      value = "${token}.dkim.amazonses.com"
      ttl   = "Automatic"
    }
  ]
}

output "dns_setup_instructions" {
  description = "Exactly what to do at Namecheap, and what NOT to touch."
  value       = <<-EOT
    Add these 3 CNAME records at your DNS host (Namecheap -> Domain List -> lubobali.com ->
    Advanced DNS -> Add New Record). Namecheap appends the domain automatically, so enter the
    Host EXACTLY as shown — do NOT append .${var.sender_domain} yourself.

    %{for token in aws_ses_domain_dkim.sender.dkim_tokens~}
    Type: CNAME | Host: ${token}._domainkey | Value: ${token}.dkim.amazonses.com | TTL: Automatic
    %{endfor~}

    SAFE FOR data@${var.sender_domain} — these are additive CNAMEs at unique random names:
      * DO NOT touch the MX records. They deliver your mail; SES sending does not need them.
      * DO NOT add a second SPF record. You already have exactly one
        ("v=spf1 include:spf.privateemail.com ~all"). A second one breaks SPF for the whole
        domain. We do not need SPF here — DKIM alignment is enough.
      * Nothing here changes how you send or receive mail today.

    Verification takes minutes to a few hours. Check with:
      aws ses get-identity-verification-attributes --region ${var.region} \
        --identities ${var.sender_domain}
      aws ses get-identity-dkim-attributes --region ${var.region} \
        --identities ${var.sender_domain}
  EOT
}
