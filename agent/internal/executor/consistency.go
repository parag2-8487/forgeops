package executor

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/parag8487/ForgeOps/agent/internal/models"
)

// VerifyConsistencyGate statically validates that the build context and referenced COPY files exist.
func VerifyConsistencyGate(repoDir string, dockerfilePath string, bp *models.ProjectBlueprint) error {
	absRepo, err := filepath.Abs(repoDir)
	if err != nil {
		return fmt.Errorf("invalid repository directory: %w", err)
	}

	absDF := dockerfilePath
	if !filepath.IsAbs(absDF) {
		absDF = filepath.Join(absRepo, dockerfilePath)
	}

	dfFile, err := os.Open(absDF)
	if err != nil {
		return fmt.Errorf("G3 consistency gate failed: Dockerfile missing at %s: %w", absDF, err)
	}
	defer dfFile.Close()

	buildContext := absRepo
	scanner := bufio.NewScanner(dfFile)
	lineNum := 0
	var inconsistencies []string

	for scanner.Scan() {
		lineNum++
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}

		upper := strings.ToUpper(line)
		if strings.HasPrefix(upper, "COPY") || strings.HasPrefix(upper, "ADD") {
			parts := strings.Fields(line)
			if len(parts) < 3 {
				continue
			}

			// Skip multi-stage copies (--from=...)
			isFromStage := false
			var sources []string
			for _, part := range parts[1 : len(parts)-1] {
				if strings.HasPrefix(part, "--from=") {
					isFromStage = true
					break
				}
				if !strings.HasPrefix(part, "--") {
					sources = append(sources, part)
				}
			}

			if isFromStage {
				continue
			}

			for _, src := range sources {
				if strings.ContainsAny(src, "*?") {
					continue
				}
				targetPath := filepath.Join(buildContext, src)
				if _, err := os.Stat(targetPath); os.IsNotExist(err) {
					inconsistencies = append(inconsistencies, fmt.Sprintf("line %d: '%s' not found in build context %s", lineNum, src, buildContext))
				}
			}
		}
	}

	if len(inconsistencies) > 0 {
		return fmt.Errorf("G3 pre-execution consistency gate failed:\n%s", strings.Join(inconsistencies, "\n"))
	}

	return nil
}
