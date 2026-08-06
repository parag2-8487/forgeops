package main
import (
	"fmt"
	"github.com/open-policy-agent/opa/bundle"
	"bytes"
)
func main() {
	fmt.Printf("%T\n", bundle.NewReader(bytes.NewReader(nil)))
}
