package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	evals "github.com/haikeilabs/agentware/go/evals"
)

var filepathGlob = filepath.Glob

func mockToolExecutor(toolName string, args map[string]interface{}) string {
	switch toolName {
	case "glob":
		pattern, _ := args["pattern"].(string)
		directory, _ := args["directory"].(string)
		if directory == "" {
			directory = "."
		}
		files, _ := filepathGlob(filepath.Join(directory, pattern))
		if len(files) > 10 {
			files = files[:10]
		}
		result := make([]string, len(files))
		copy(result, files)
		data, _ := json.Marshal(result)
		return string(data)

	case "read_file":
		path, _ := args["path"].(string)
		data, err := os.ReadFile(path)
		if err != nil {
			return fmt.Sprintf("File not found: %s", path)
		}
		if len(data) > 1000 {
			return string(data[:1000])
		}
		return string(data)

	case "search_files":
		data, _ := json.Marshal([]string{"match in file1.go", "match in file2.go"})
		return string(data)

	case "calculator":
		expr, _ := args["expression"].(string)
		result, err := evalExpr(expr)
		if err != nil {
			return fmt.Sprintf("Error: %v", err)
		}
		return fmt.Sprintf("%v", result)

	case "get_weather":
		location, _ := args["location"].(string)
		data, _ := json.Marshal(map[string]interface{}{
			"location":  location,
			"temp":      72,
			"condition": "sunny",
		})
		return string(data)

	case "translate":
		text, _ := args["text"].(string)
		targetLang, _ := args["target_lang"].(string)
		data, _ := json.Marshal(map[string]interface{}{
			"original":   text,
			"translated": fmt.Sprintf("[translated: %s]", text),
			"target":     targetLang,
		})
		return string(data)
	}

	return fmt.Sprintf("Mock result for %s", toolName)
}

func evalExpr(expr string) (interface{}, error) {
	var a, b float64
	var op byte
	_, err := fmt.Sscanf(expr, "%f %c %f", &a, &op, &b)
	if err != nil {
		return nil, fmt.Errorf("invalid expression: %s", expr)
	}

	switch op {
	case '+':
		return a + b, nil
	case '-':
		return a - b, nil
	case '*':
		return a * b, nil
	case '/':
		if b == 0 {
			return nil, fmt.Errorf("division by zero")
		}
		return a / b, nil
	}
	return nil, fmt.Errorf("unsupported operator: %c", op)
}

func main() {
	fileSearch := flag.Bool("file-search", false, "Run file search evals only")
	general := flag.Bool("general", false, "Run general tool calling evals only")
	_ = flag.Bool("all", true, "Run all evals (default)")
	models := flag.String("models", "qwen3.6-27b-mtp", "Comma-separated model list")
	baseURL := flag.String("base-url", "http://pedrogpt:8000", "API base URL")
	maxTurns := flag.Int("max-turns", 10, "Max turns per eval")
	flag.Parse()

	var cases []evals.EvalCase
	var outputFile string

	if *fileSearch {
		cases = evals.FileSearchCases
		outputFile = "file_search_results.json"
	} else if *general {
		cases = evals.GeneralCases
		outputFile = "general_results.json"
	} else {
		cases = append(evals.FileSearchCases, evals.GeneralCases...)
		outputFile = "results.json"
	}

	var modelList []string
	if *models != "" {
		modelList = splitAndTrim(*models)
	}

	runner := evals.NewEvalRunner(*baseURL, *maxTurns)
	report := runner.RunEvals(cases, modelList, mockToolExecutor)

	outputDir := filepath.Join("python", "src", "evals", "output")
	if err := os.MkdirAll(outputDir, 0755); err != nil {
		fmt.Fprintf(os.Stderr, "Warning: failed to create output dir: %v\n", err)
	}
	outputPath := filepath.Join(outputDir, outputFile)
	runner.SaveReport(report, outputPath)

	fmt.Println("\n=== Summary ===")
	for _, model := range modelList {
		passRate := report.PassRate(model) * 100
		fmt.Printf("%s: %.1f%% pass rate\n", model, passRate)
	}

	fmt.Printf("\nResults saved to: %s\n", outputPath)
}

func splitAndTrim(s string) []string {
	var result []string
	for _, part := range split(s, ",") {
		trimmed := trim(part)
		if trimmed != "" {
			result = append(result, trimmed)
		}
	}
	return result
}

func split(s, sep string) []string {
	if s == "" {
		return nil
	}
	var result []string
	start := 0
	for i := 0; i <= len(s)-len(sep); i++ {
		if s[i:i+len(sep)] == sep {
			result = append(result, s[start:i])
			start = i + len(sep)
			i += len(sep) - 1
		}
	}
	result = append(result, s[start:])
	return result
}

func trim(s string) string {
	start := 0
	end := len(s)
	for start < end && (s[start] == ' ' || s[start] == '\t') {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == '\t') {
		end--
	}
	return s[start:end]
}
