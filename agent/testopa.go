package main

import (
	"bytes"
	"fmt"
	"github.com/open-policy-agent/opa/bundle"
)

func main() {
	fmt.Printf("%T\n", bundle.NewReader(bytes.NewReader(nil)))
}
