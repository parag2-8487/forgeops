// SPDX-License-Identifier: Apache-2.0
package logging

import (
	"errors"
	"fmt"
	"regexp"
	"strings"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

// New creates a zap.Logger configured for the given level and format.
// format must be "console" (dev) or "json" (prod).
func New(level, format string) (*zap.Logger, error) {
	lvl, err := zapcore.ParseLevel(level)
	if err != nil {
		lvl = zapcore.InfoLevel
	}

	var cfg zap.Config
	switch format {
	case "json":
		cfg = zap.NewProductionConfig()
	default:
		cfg = zap.NewDevelopmentConfig()
	}
	cfg.Level = zap.NewAtomicLevelAt(lvl)
	cfg.EncoderConfig.TimeKey = "ts"
	cfg.EncoderConfig.LevelKey = "level"
	cfg.EncoderConfig.MessageKey = "msg"
	cfg.EncoderConfig.CallerKey = "caller"
	cfg.EncoderConfig.EncodeTime = zapcore.ISO8601TimeEncoder

	logger, err := cfg.Build(zap.AddCallerSkip(0))
	if err != nil {
		return nil, err
	}
	return logger, nil
}

// NewRedacted creates a logger that scrubs bearer tokens and configured secret
// values BEFORE encoding. Secrets are replaced with "[REDACTED]".
func NewRedacted(level, format string, secrets []string) (*zap.Logger, error) {
	lvl, err := zapcore.ParseLevel(level)
	if err != nil {
		lvl = zapcore.InfoLevel
	}

	var encCfg zapcore.EncoderConfig
	switch format {
	case "json":
		encCfg = zap.NewProductionEncoderConfig()
	default:
		encCfg = zap.NewDevelopmentEncoderConfig()
	}
	encCfg.TimeKey = "ts"
	encCfg.LevelKey = "level"
	encCfg.MessageKey = "msg"
	encCfg.CallerKey = "caller"
	encCfg.EncodeTime = zapcore.ISO8601TimeEncoder

	var enc zapcore.Encoder
	if format == "json" {
		enc = zapcore.NewJSONEncoder(encCfg)
	} else {
		enc = zapcore.NewConsoleEncoder(encCfg)
	}

	sink, closeOut, err := zap.Open("stderr")
	if err != nil {
		return nil, err
	}
	_ = closeOut // Logger owns closing

	core := zapcore.NewCore(enc, sink, lvl)
	redactingCore := &redactCore{
		Core:    core,
		secrets: secrets,
	}

	logger := zap.New(redactingCore, zap.AddCaller())
	return logger, nil
}

// redactCore wraps a zapcore.Core and redacts secrets from fields before encoding.
type redactCore struct {
	zapcore.Core
	secrets []string
}

var bearerPattern = regexp.MustCompile(`(?i)(bearer\s+)\S+`)

func (c *redactCore) Check(entry zapcore.Entry, ce *zapcore.CheckedEntry) *zapcore.CheckedEntry {
	// Redact the message itself
	entry.Message = c.redactString(entry.Message)
	if c.Core.Enabled(entry.Level) {
		return ce.AddCore(entry, c)
	}
	return ce
}

func (c *redactCore) Write(entry zapcore.Entry, fields []zapcore.Field) error {
	entry.Message = c.redactString(entry.Message)
	redacted := make([]zapcore.Field, len(fields))
	for i, f := range fields {
		redacted[i] = c.redactField(f)
	}
	return c.Core.Write(entry, redacted)
}

func (c *redactCore) With(fields []zapcore.Field) zapcore.Core {
	redacted := make([]zapcore.Field, len(fields))
	for i, f := range fields {
		redacted[i] = c.redactField(f)
	}
	return &redactCore{
		Core:    c.Core.With(redacted),
		secrets: c.secrets,
	}
}

func (c *redactCore) redactString(s string) string {
	// Redact bearer tokens
	s = bearerPattern.ReplaceAllString(s, "${1}[REDACTED]")
	// Redact configured secrets
	for _, secret := range c.secrets {
		if secret != "" && strings.Contains(s, secret) {
			s = strings.ReplaceAll(s, secret, "[REDACTED]")
		}
	}
	return s
}

// redactField scrubs every field kind that can carry a secret.
//
// This handled `StringType` only, which left the likeliest leak of all wide open: an
// ERROR field. Transport libraries put the URL — with its embedded credential — into
// the error message, so `zap.Error(err)` after a failed git push wrote the token
// verbatim. That is precisely the defect D-27 repaired on the Python side, where
// `JSONFormatter` scrubbed `record.msg` and then emitted unredacted `formatException`
// output; the Go side had the same shape and no test had asked.
//
// The kinds below are the ones that carry caller-supplied text. `ReflectType` and
// `AnyType` are handled by rendering and comparing: if the rendered form contains a
// secret the field is replaced wholesale with a redacted string, because losing the
// structure of one log field is unambiguously better than emitting a credential.
// Numeric and boolean kinds are left alone — they cannot contain a substring.
func (c *redactCore) redactField(f zapcore.Field) zapcore.Field {
	switch f.Type {
	case zapcore.StringType:
		f.String = c.redactString(f.String)

	case zapcore.ByteStringType:
		if raw, ok := f.Interface.([]byte); ok {
			f.Interface = []byte(c.redactString(string(raw)))
		}

	case zapcore.ErrorType:
		if err, ok := f.Interface.(error); ok && err != nil {
			if cleaned := c.redactString(err.Error()); cleaned != err.Error() {
				// Replaced with a plain error carrying the scrubbed text. Keeping the
				// original and hoping the encoder does not reach Error() would leave
				// the leak one encoder change away.
				f.Interface = errors.New(cleaned)
			}
		}

	case zapcore.StringerType:
		if s, ok := f.Interface.(fmt.Stringer); ok && s != nil {
			if cleaned := c.redactString(s.String()); cleaned != s.String() {
				f.Type = zapcore.StringType
				f.Interface = nil
				f.String = cleaned
			}
		}

	case zapcore.ReflectType:
		// Rendering an arbitrary value is not free, so it happens only when a secret is
		// actually configured, and the field is rewritten only when one is present.
		if len(c.secrets) > 0 && f.Interface != nil {
			rendered := fmt.Sprint(f.Interface)
			if cleaned := c.redactString(rendered); cleaned != rendered {
				f.Type = zapcore.StringType
				f.Interface = nil
				f.String = cleaned
			}
		}
	}
	return f
}
