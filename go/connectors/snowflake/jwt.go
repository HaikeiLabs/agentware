// Package snowflake mints the short-lived key-pair JWT that Snowflake's SQL
// API accepts as a bearer token.
//
// It is a pure helper for the harness side of a Snowflake connector: the Kei
// runtime releases the service user's private key (a PEM, delivered as the
// released credential's access token) for one call, and the harness turns it
// into a JWT here, then calls Snowflake itself. The package makes no network
// calls, never logs, and never puts key material or the token in an error.
//
// Token shape (Snowflake key-pair authentication):
//
//	header  {"alg":"RS256","typ":"JWT"}
//	iss     <ACCOUNT>.<USER>.SHA256:<base64 SHA-256 of the public key DER>
//	sub     <ACCOUNT>.<USER>
//	iat     now, in Unix seconds
//	exp     iat + ttl, capped at one hour
//
// Send it as "Authorization: Bearer <token>"; the optional
// "X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT" header names the type.
package snowflake

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"strings"
	"time"
	"unicode"
)

// MaxTTL is the longest lifetime Snowflake honours for a key-pair JWT. A
// longer ttl is capped to it.
const MaxTTL = time.Hour

// MinRSABits is the smallest RSA modulus accepted.
const MinRSABits = 2048

// Errors returned by this package. Their messages are fixed: they never carry
// the key, the PEM, the token, or a parser's error text.
var (
	// ErrInvalidAccount means the account identifier is empty, is a host name,
	// or contains characters outside A-Z, 0-9, '_' and '-' after normalizing.
	ErrInvalidAccount = errors.New("snowflake: invalid account identifier")
	// ErrInvalidUser means the user is empty or contains control characters.
	ErrInvalidUser = errors.New("snowflake: invalid user")
	// ErrInvalidTTL means the requested lifetime is zero or negative.
	ErrInvalidTTL = errors.New("snowflake: ttl must be positive")
	// ErrInvalidKey means the key is not an unencrypted RSA private key of at
	// least MinRSABits bits in PKCS#8 or PKCS#1 PEM, or the public key is nil.
	ErrInvalidKey = errors.New("snowflake: invalid RSA key")
	// ErrEncryptedPrivateKey means the PEM is passphrase-encrypted. This
	// package takes no passphrase; store the service key unencrypted in the
	// tenant secret store.
	ErrEncryptedPrivateKey = errors.New("snowflake: private key is encrypted; passphrase-protected keys are not supported")
)

// NormalizeAccountIdentifier returns the account identifier in the form the
// JWT claims need.
//
// It trims and upper-cases the input. An account locator with region and
// cloud suffixes ("xy12345.us-east-1.aws") is cut at the first '.', leaving
// the locator. An identifier containing ".global" has every '.' replaced by
// '-'. The identifier is never cut at '-', so the organization form is kept
// whole: pass it as ORG-ACCOUNT, not ORG.ACCOUNT (the dotted form would be
// read as locator.region). A host name (…snowflakecomputing.com) is refused.
func NormalizeAccountIdentifier(id string) (string, error) {
	s := strings.ToUpper(strings.TrimSpace(id))
	if strings.Contains(s, "SNOWFLAKECOMPUTING") {
		return "", ErrInvalidAccount
	}
	if strings.Contains(s, ".GLOBAL") {
		s = strings.ReplaceAll(s, ".", "-")
	} else if i := strings.IndexByte(s, '.'); i >= 0 {
		s = s[:i]
	}
	if s == "" {
		return "", ErrInvalidAccount
	}
	for _, r := range s {
		if !isAccountRune(r) {
			return "", ErrInvalidAccount
		}
	}
	return s, nil
}

func isAccountRune(r rune) bool {
	return r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-'
}

func normalizeUser(user string) (string, error) {
	s := strings.ToUpper(strings.TrimSpace(user))
	if s == "" {
		return "", ErrInvalidUser
	}
	for _, r := range s {
		if unicode.IsControl(r) {
			return "", ErrInvalidUser
		}
	}
	return s, nil
}

// PublicKeyFingerprint returns "SHA256:" followed by the standard base64 of
// the SHA-256 of the key's DER SubjectPublicKeyInfo. It equals Snowflake's
// RSA_PUBLIC_KEY_FP and
// "openssl rsa -pubout -outform DER | openssl dgst -sha256 -binary | base64".
func PublicKeyFingerprint(pub *rsa.PublicKey) (string, error) {
	if pub == nil || pub.N == nil {
		return "", ErrInvalidKey
	}
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		return "", ErrInvalidKey
	}
	sum := sha256.Sum256(der)
	return "SHA256:" + base64.StdEncoding.EncodeToString(sum[:]), nil
}

// parsePrivateKey decodes an unencrypted PKCS#8 ("PRIVATE KEY") or PKCS#1
// ("RSA PRIVATE KEY") PEM into an RSA key of at least MinRSABits bits.
func parsePrivateKey(privateKeyPEM []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(privateKeyPEM)
	if block == nil {
		return nil, ErrInvalidKey
	}
	if block.Type == "ENCRYPTED PRIVATE KEY" || strings.Contains(block.Headers["Proc-Type"], "ENCRYPTED") {
		return nil, ErrEncryptedPrivateKey
	}
	var key *rsa.PrivateKey
	switch block.Type {
	case "PRIVATE KEY":
		parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
		if err != nil {
			return nil, ErrInvalidKey
		}
		rsaKey, ok := parsed.(*rsa.PrivateKey)
		if !ok {
			return nil, ErrInvalidKey
		}
		key = rsaKey
	case "RSA PRIVATE KEY":
		parsed, err := x509.ParsePKCS1PrivateKey(block.Bytes)
		if err != nil {
			return nil, ErrInvalidKey
		}
		key = parsed
	default:
		return nil, ErrInvalidKey
	}
	if key.N.BitLen() < MinRSABits {
		return nil, ErrInvalidKey
	}
	return key, nil
}

type jwtClaims struct {
	Iss string `json:"iss"`
	Sub string `json:"sub"`
	Iat int64  `json:"iat"`
	Exp int64  `json:"exp"`
}

// rs256Header is the fixed, pre-encoded JWT header {"alg":"RS256","typ":"JWT"}.
var rs256Header = base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))

// JWT mints a Snowflake key-pair JWT for user in accountIdentifier, signed
// with privateKeyPEM (an unencrypted PKCS#8 or PKCS#1 RSA key). iat is now
// truncated to whole seconds; exp is iat plus ttl, capped at MaxTTL. A ttl of
// zero or less is refused. See NormalizeAccountIdentifier for the account
// rules; the user is trimmed and upper-cased.
//
// The returned token is a credential: never log it.
func JWT(accountIdentifier, user string, privateKeyPEM []byte, now time.Time, ttl time.Duration) (string, error) {
	account, err := NormalizeAccountIdentifier(accountIdentifier)
	if err != nil {
		return "", err
	}
	u, err := normalizeUser(user)
	if err != nil {
		return "", err
	}
	if ttl <= 0 {
		return "", ErrInvalidTTL
	}
	if ttl > MaxTTL {
		ttl = MaxTTL
	}
	key, err := parsePrivateKey(privateKeyPEM)
	if err != nil {
		return "", err
	}
	fp, err := PublicKeyFingerprint(&key.PublicKey)
	if err != nil {
		return "", err
	}

	iat := now.Unix()
	subject := account + "." + u
	payload, err := json.Marshal(jwtClaims{
		Iss: subject + "." + fp,
		Sub: subject,
		Iat: iat,
		Exp: iat + int64(ttl/time.Second),
	})
	if err != nil {
		return "", errors.New("snowflake: encode claims")
	}
	signingInput := rs256Header + "." + base64.RawURLEncoding.EncodeToString(payload)
	digest := sha256.Sum256([]byte(signingInput))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, digest[:])
	if err != nil {
		return "", errors.New("snowflake: sign token")
	}
	return signingInput + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}
