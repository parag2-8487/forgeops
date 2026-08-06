package forgeops.governance

default decision = "deny"

decision = "allow" { input.action == "allow_me" }
