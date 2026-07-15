# The verified sender/recipient for the digest and instant alerts.
#
# Phase 0's checklist claimed an identity was verified; it was not — the account had zero SES
# identities when Terraform first ran. Provisioned here, not remembered.

# --- The address identity (verified; kept as the RECIPIENT) --------------------------------
#
# Proves we own the inbox, which is what the SES sandbox requires of a recipient. It does NOT
# make SES an authorised sender for gmail.com — see the domain identity below for why that
# matters.
resource "aws_ses_email_identity" "digest_recipient" {
  email = var.digest_email
}

# --- The domain identity (the SENDER) ------------------------------------------------------
#
# Why this exists: sending AS a @gmail.com address through SES is unauthenticated spoofing from
# Gmail's point of view. The mail claims to be from gmail.com but arrives from Amazon's servers,
# Amazon is not an authorised sender for gmail.com, SPF/DKIM fail, and Gmail files it as spam.
# Measured: our first send was accepted by SES with 0 bounces and still never reached the inbox.
#
# Sending from a domain Lubo actually owns fixes it: Easy DKIM signs the mail with lubobali.com,
# the signature aligns with the From address, and it lands in the inbox.
resource "aws_ses_domain_identity" "sender" {
  domain = var.sender_domain
}

# Easy DKIM: AWS returns three tokens, each published as a CNAME.
#
# SAFE BY CONSTRUCTION for the existing mailbox (data@lubobali.com, on Namecheap Private Email):
#   * these are CNAMEs at unique random names (<token>._domainkey.lubobali.com) — nothing else
#     uses them, so they collide with nothing;
#   * MX is NOT touched, so mail delivery is completely unaffected;
#   * SPF is NOT touched. The domain already has exactly one SPF record
#     ("v=spf1 include:spf.privateemail.com ~all") and a SECOND one would break SPF entirely
#     (the spec permits one; two is a permerror). DKIM alignment alone satisfies DMARC, so
#     there is no reason to go near it. If SPF is ever wanted, EDIT that single record to add
#     `include:amazonses.com` — never add another.
resource "aws_ses_domain_dkim" "sender" {
  domain = aws_ses_domain_identity.sender.domain
}

# --- Where the digest is sent FROM ---------------------------------------------------------
#
# `jobs@lubobali.com` needs no mailbox: it is a sending identity only. Replies are not expected
# — the app talks to Lubo, not the other way round.
locals {
  sender_address = "${var.sender_local_part}@${var.sender_domain}"
}
