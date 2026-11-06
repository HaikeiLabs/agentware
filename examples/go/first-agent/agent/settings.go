package agent

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// DefaultBaseURL is used when OPENAI_BASE_URL is not set.
const DefaultBaseURL = "https://api.openai.com/v1"

// ModelTimeout bounds one model request. Local models can take a minute or
// more to write the summary, so it is longer than the library default.
const ModelTimeout = 3 * time.Minute

// Settings are the model endpoint settings read from the environment.
type Settings struct {
	BaseURL string
	APIKey  string
	Model   string
}

// SettingsFromEnv reads OPENAI_API_KEY and MODEL (both required) and
// OPENAI_BASE_URL (optional) through getenv, usually os.Getenv.
func SettingsFromEnv(getenv func(string) string) (Settings, error) {
	s := Settings{
		BaseURL: strings.TrimSpace(getenv("OPENAI_BASE_URL")),
		APIKey:  strings.TrimSpace(getenv("OPENAI_API_KEY")),
		Model:   strings.TrimSpace(getenv("MODEL")),
	}
	if s.BaseURL == "" {
		s.BaseURL = DefaultBaseURL
	}
	var missing []string
	if s.APIKey == "" {
		missing = append(missing, "OPENAI_API_KEY")
	}
	if s.Model == "" {
		missing = append(missing, "MODEL")
	}
	if len(missing) > 0 {
		return Settings{}, fmt.Errorf("%w: set %s", ErrMissingSetting, strings.Join(missing, " and "))
	}
	return s, nil
}

// ErrMissingSetting is returned when a required environment variable is unset.
var ErrMissingSetting = errors.New("missing setting")
