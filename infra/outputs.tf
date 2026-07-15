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

output "ses_identity" {
  description = "SES sender identity. Unverified until the confirmation link is clicked."
  value       = aws_ses_email_identity.digest.email
}

output "next_steps" {
  description = "What terraform cannot do for you."
  value       = <<-EOT
    1. Click the SES verification link AWS emailed to ${var.digest_email}. SES refuses to send
       until it is clicked, and terraform cannot click it.
    2. Write the real Adzuna secrets. They are kept out of terraform state on purpose — state
       stores values in PLAINTEXT, even for a SecureString (see ssm.tf):
         aws ssm put-parameter --region ${var.region} --overwrite \
           --name ${aws_ssm_parameter.adzuna_app_id.name} --type String --value "<app_id>"
         aws ssm put-parameter --region ${var.region} --overwrite \
           --name ${aws_ssm_parameter.adzuna_app_key.name} --type SecureString --value "<app_key>"
  EOT
}
