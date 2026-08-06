// SPDX-License-Identifier: Apache-2.0
package validator

import (
	"testing"
)

func TestOpenTofuValidator_ValidHCL(t *testing.T) {
	v := NewOpenTofuValidator("tofu")
	hcl := `
resource "aws_s3_bucket" "b" {
  bucket = "my-tf-test-bucket"
}
`
	if err := v.ValidateHCLContent(hcl); err != nil {
		t.Fatalf("expected valid HCL content, got err: %v", err)
	}
}

func TestOpenTofuValidator_MissingBlocks(t *testing.T) {
	v := NewOpenTofuValidator("tofu")
	hcl := "some_random_key = 123"
	if err := v.ValidateHCLContent(hcl); err == nil {
		t.Fatalf("expected error for non-HCL content, got nil")
	}
}
