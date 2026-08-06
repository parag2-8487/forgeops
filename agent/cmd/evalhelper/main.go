package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"github.com/parag8487/ForgeOps/agent/internal/policy"
)

func main() {
	bundlePath := flag.String("bundle", "", "Path to the OPA bundle tar.gz file")
	inputJSON := flag.String("input", "", "JSON input string")
	expectedDigest := flag.String("expected-digest", "", "Expected bundle digest for drift check")
	flag.Parse()

	if *bundlePath == "" || *inputJSON == "" {
		fmt.Println("Usage: evalhelper -bundle <path> -input <json> [-expected-digest <digest>]")
		os.Exit(1)
	}

	bundleData, err := os.ReadFile(*bundlePath)
	if err != nil {
		fmt.Printf("{\"error\": \"failed to read bundle: %v\"}\n", err)
		os.Exit(1)
	}

	evaluator := policy.NewEvaluator()
	err = evaluator.Load(context.Background(), bundleData)
	if err != nil {
		fmt.Printf("{\"error\": \"failed to load bundle: %v\"}\n", err)
		os.Exit(1)
	}

	var input map[string]interface{}
	if err := json.Unmarshal([]byte(*inputJSON), &input); err != nil {
		fmt.Printf("{\"error\": \"invalid json input: %v\"}\n", err)
		os.Exit(1)
	}

	var digest string
	if *expectedDigest != "" {
		digest = *expectedDigest
	} else {
		hash := sha256.Sum256(bundleData)
		digest = hex.EncodeToString(hash[:])
	}

	decision, err := evaluator.Evaluate(context.Background(), input, digest)
	if err != nil {
		// Output JSON so python can parse it easily even on error
		out, _ := json.Marshal(map[string]interface{}{"decision": decision, "error": err.Error()})
		fmt.Println(string(out))
		os.Exit(0)
	}

	out, _ := json.Marshal(map[string]interface{}{"decision": decision})
	fmt.Println(string(out))
}
