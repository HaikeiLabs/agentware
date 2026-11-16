package snowflake

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fixtureDir holds the cross-language Snowflake JWT fixtures at the
// repository root, outside the go/ module.
const fixtureDir = "../../../fixtures/snowflake"

type claimCase struct {
	Name       string `json:"name"`
	Account    string `json:"account"`
	User       string `json:"user"`
	NowUnix    int64  `json:"now_unix"`
	TTLSeconds int64  `json:"ttl_seconds"`
	Iss        string `json:"iss"`
	Sub        string `json:"sub"`
	Iat        int64  `json:"iat"`
	Exp        int64  `json:"exp"`
}

type inOut struct {
	In  string `json:"in"`
	Out string `json:"out"`
}

type cases struct {
	Keys struct {
		PKCS8          string `json:"pkcs8"`
		PKCS1          string `json:"pkcs1"`
		Public         string `json:"public"`
		EncryptedPKCS8 string `json:"encrypted_pkcs8"`
		EncryptedPKCS1 string `json:"encrypted_pkcs1"`
	} `json:"keys"`
	ExpectedFingerprint string      `json:"expected_fingerprint"`
	Claims              []claimCase `json:"claims"`
	InvalidTTLSeconds   []int64     `json:"invalid_ttl_seconds"`
	Accounts            []inOut     `json:"accounts"`
	InvalidAccounts     []string    `json:"invalid_accounts"`
	Users               []inOut     `json:"users"`
	InvalidUsers        []string    `json:"invalid_users"`
}

func loadCases(t *testing.T) cases {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(fixtureDir, "cases.v1.json"))
	if err != nil {
		t.Fatalf("read cases: %v", err)
	}
	var c cases
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatalf("parse cases: %v", err)
	}
	return c
}

func readFixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(fixtureDir, name))
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	return raw
}

func fixturePublicKey(t *testing.T, c cases) *rsa.PublicKey {
	t.Helper()
	block, _ := pem.Decode(readFixture(t, c.Keys.Public))
	if block == nil {
		t.Fatal("public key fixture is not PEM")
	}
	pub, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		t.Fatalf("parse public key: %v", err)
	}
	return pub.(*rsa.PublicKey)
}

type parsedJWT struct {
	header  map[string]any
	claims  map[string]any
	signed  string
	sigUsed []byte
}

func splitJWT(t *testing.T, token string) parsedJWT {
	t.Helper()
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		t.Fatalf("token has %d parts, want 3", len(parts))
	}
	dec := func(s string) []byte {
		b, err := base64.RawURLEncoding.DecodeString(s)
		if err != nil {
			t.Fatalf("segment is not unpadded base64url: %v", err)
		}
		return b
	}
	var p parsedJWT
	if err := json.Unmarshal(dec(parts[0]), &p.header); err != nil {
		t.Fatalf("header: %v", err)
	}
	if err := json.Unmarshal(dec(parts[1]), &p.claims); err != nil {
		t.Fatalf("claims: %v", err)
	}
	p.signed = parts[0] + "." + parts[1]
	p.sigUsed = dec(parts[2])
	return p
}

func verifyRS256(t *testing.T, pub *rsa.PublicKey, p parsedJWT) {
	t.Helper()
	digest := sha256.Sum256([]byte(p.signed))
	if err := rsa.VerifyPKCS1v15(pub, crypto.SHA256, digest[:], p.sigUsed); err != nil {
		t.Fatalf("RS256 signature does not verify: %v", err)
	}
}

func encodePKCS8(t *testing.T, key any) []byte {
	t.Helper()
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
}

func TestPublicKeyFingerprintMatchesOpenSSL(t *testing.T) {
	c := loadCases(t)
	got, err := PublicKeyFingerprint(fixturePublicKey(t, c))
	if err != nil {
		t.Fatalf("PublicKeyFingerprint: %v", err)
	}
	if got != c.ExpectedFingerprint {
		t.Fatalf("fingerprint = %q, want %q", got, c.ExpectedFingerprint)
	}
}

func TestPublicKeyFingerprintRejectsNil(t *testing.T) {
	if _, err := PublicKeyFingerprint(nil); !errors.Is(err, ErrInvalidKey) {
		t.Fatalf("err = %v, want ErrInvalidKey", err)
	}
}

func TestJWTClaimsExact(t *testing.T) {
	c := loadCases(t)
	pub := fixturePublicKey(t, c)
	for _, keyFile := range []string{c.Keys.PKCS8, c.Keys.PKCS1} {
		for _, tc := range c.Claims {
			t.Run(keyFile+"/"+tc.Name, func(t *testing.T) {
				token, err := JWT(tc.Account, tc.User, readFixture(t, keyFile),
					time.Unix(tc.NowUnix, 0).UTC(), time.Duration(tc.TTLSeconds)*time.Second)
				if err != nil {
					t.Fatalf("JWT: %v", err)
				}
				p := splitJWT(t, token)
				if p.header["alg"] != "RS256" || p.header["typ"] != "JWT" || len(p.header) != 2 {
					t.Fatalf("header = %v, want exactly alg=RS256 typ=JWT", p.header)
				}
				want := map[string]any{
					"iss": tc.Iss, "sub": tc.Sub,
					"iat": float64(tc.Iat), "exp": float64(tc.Exp),
				}
				if len(p.claims) != len(want) {
					t.Fatalf("claims = %v, want exactly %v", p.claims, want)
				}
				for k, v := range want {
					if p.claims[k] != v {
						t.Fatalf("claim %s = %v, want %v", k, p.claims[k], v)
					}
				}
				verifyRS256(t, pub, p)
			})
		}
	}
}

func TestJWTTruncatesSubSecondNow(t *testing.T) {
	c := loadCases(t)
	now := time.Unix(1700000000, 999_000_000)
	token, err := JWT("xy12345", "u", readFixture(t, c.Keys.PKCS8), now, time.Minute)
	if err != nil {
		t.Fatalf("JWT: %v", err)
	}
	p := splitJWT(t, token)
	if p.claims["iat"] != float64(1700000000) || p.claims["exp"] != float64(1700000060) {
		t.Fatalf("iat/exp = %v/%v", p.claims["iat"], p.claims["exp"])
	}
}

func TestJWTSignatureVerifiesWithGeneratedKey(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	token, err := JWT("myorg-myaccount", "svc", encodePKCS8(t, key), time.Now(), 10*time.Minute)
	if err != nil {
		t.Fatalf("JWT: %v", err)
	}
	p := splitJWT(t, token)
	verifyRS256(t, &key.PublicKey, p)

	fp, err := PublicKeyFingerprint(&key.PublicKey)
	if err != nil {
		t.Fatalf("fingerprint: %v", err)
	}
	if p.claims["iss"] != "MYORG-MYACCOUNT.SVC."+fp {
		t.Fatalf("iss = %v", p.claims["iss"])
	}

	other, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	digest := sha256.Sum256([]byte(p.signed))
	if rsa.VerifyPKCS1v15(&other.PublicKey, crypto.SHA256, digest[:], p.sigUsed) == nil {
		t.Fatal("signature verified with an unrelated key")
	}
}

func TestJWTRejectsInvalidTTL(t *testing.T) {
	c := loadCases(t)
	for _, s := range c.InvalidTTLSeconds {
		_, err := JWT("xy12345", "u", readFixture(t, c.Keys.PKCS8), time.Now(), time.Duration(s)*time.Second)
		if !errors.Is(err, ErrInvalidTTL) {
			t.Fatalf("ttl %ds: err = %v, want ErrInvalidTTL", s, err)
		}
	}
}

func TestJWTRejectsEncryptedPEM(t *testing.T) {
	c := loadCases(t)
	for _, f := range []string{c.Keys.EncryptedPKCS8, c.Keys.EncryptedPKCS1} {
		_, err := JWT("xy12345", "u", readFixture(t, f), time.Now(), time.Minute)
		if !errors.Is(err, ErrEncryptedPrivateKey) {
			t.Fatalf("%s: err = %v, want ErrEncryptedPrivateKey", f, err)
		}
		if !strings.Contains(err.Error(), "encrypted") || !strings.Contains(err.Error(), "passphrase") {
			t.Fatalf("%s: error %q should say the key is encrypted and no passphrase is supported", f, err)
		}
	}
}

func TestJWTRejectsInvalidKeys(t *testing.T) {
	small, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	ec, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("generate: %v", err)
	}
	c := loadCases(t)
	inputs := map[string][]byte{
		"empty":       nil,
		"not pem":     []byte("not a key"),
		"public key":  readFixture(t, c.Keys.Public),
		"rsa 1024":    encodePKCS8(t, small),
		"ec p256":     encodePKCS8(t, ec),
		"garbage der": pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: []byte("junk")}),
	}
	for name, in := range inputs {
		_, err := JWT("xy12345", "u", in, time.Now(), time.Minute)
		if !errors.Is(err, ErrInvalidKey) {
			t.Fatalf("%s: err = %v, want ErrInvalidKey", name, err)
		}
	}
}

func TestErrorsNeverCarryKeyMaterial(t *testing.T) {
	c := loadCases(t)
	key := readFixture(t, c.Keys.PKCS8)
	block, _ := pem.Decode(key)
	body := base64.StdEncoding.EncodeToString(block.Bytes)

	// Corrupt one byte deep inside the key so parsing fails after decoding.
	corrupt := append([]byte(nil), block.Bytes...)
	corrupt[len(corrupt)/2] ^= 0xff
	bad := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: corrupt})

	attempts := []error{}
	for _, in := range [][]byte{bad, key[:len(key)/2], readFixture(t, c.Keys.EncryptedPKCS8)} {
		_, err := JWT("xy12345", "u", in, time.Now(), time.Minute)
		attempts = append(attempts, err)
	}
	_, err := JWT("", "u", key, time.Now(), time.Minute)
	attempts = append(attempts, err)
	_, err = JWT("xy12345", "u", key, time.Now(), 0)
	attempts = append(attempts, err)

	for i, err := range attempts {
		if err == nil {
			t.Fatalf("attempt %d: expected an error", i)
		}
		msg := err.Error()
		if strings.Contains(msg, "BEGIN") || strings.Contains(msg, "PRIVATE") ||
			strings.Contains(msg, body[:32]) || strings.Contains(msg, body[len(body)/2:len(body)/2+32]) {
			t.Fatalf("attempt %d: error leaks key material: %q", i, msg)
		}
	}
}

func TestNormalizeAccountIdentifier(t *testing.T) {
	c := loadCases(t)
	for _, tc := range c.Accounts {
		got, err := NormalizeAccountIdentifier(tc.In)
		if err != nil {
			t.Fatalf("%q: %v", tc.In, err)
		}
		if got != tc.Out {
			t.Fatalf("%q -> %q, want %q", tc.In, got, tc.Out)
		}
	}
	for _, in := range c.InvalidAccounts {
		if _, err := NormalizeAccountIdentifier(in); !errors.Is(err, ErrInvalidAccount) {
			t.Fatalf("%q: err = %v, want ErrInvalidAccount", in, err)
		}
	}
}

func TestJWTNormalizesUser(t *testing.T) {
	c := loadCases(t)
	key := readFixture(t, c.Keys.PKCS8)
	for _, tc := range c.Users {
		token, err := JWT("xy12345", tc.In, key, time.Unix(1700000000, 0), time.Minute)
		if err != nil {
			t.Fatalf("%q: %v", tc.In, err)
		}
		if sub := splitJWT(t, token).claims["sub"]; sub != "XY12345."+tc.Out {
			t.Fatalf("%q: sub = %v", tc.In, sub)
		}
	}
	for _, in := range c.InvalidUsers {
		if _, err := JWT("xy12345", in, key, time.Now(), time.Minute); !errors.Is(err, ErrInvalidUser) {
			t.Fatalf("%q: err = %v, want ErrInvalidUser", in, err)
		}
	}
}
