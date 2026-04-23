package backend

import (
	"crypto/hmac"
	"crypto/sha1"
	"encoding/base32"
	"encoding/binary"
	"fmt"
	"strings"
	"time"
)

const (
	spotifyTOTPSecret  = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
	spotifyTOTPVersion = 61
)

// generateSpotifyTOTP returns a 6-digit TOTP code, version, and any error.
// It uses the hardcoded Spotify web-player secret (RFC 6238 / HMAC-SHA1).
func generateSpotifyTOTP(t time.Time) (string, int, error) {
	padded := strings.ToUpper(spotifyTOTPSecret)
	if rem := len(padded) % 8; rem != 0 {
		padded += strings.Repeat("=", 8-rem)
	}

	key, err := base32.StdEncoding.DecodeString(padded)
	if err != nil {
		return "", 0, fmt.Errorf("totp: base32 decode failed: %w", err)
	}

	counter := uint64(t.Unix() / 30)
	msg := make([]byte, 8)
	binary.BigEndian.PutUint64(msg, counter)

	mac := hmac.New(sha1.New, key)
	mac.Write(msg)
	h := mac.Sum(nil)

	offset := h[len(h)-1] & 0x0F
	code := binary.BigEndian.Uint32(h[offset:offset+4]) & 0x7FFFFFFF

	return fmt.Sprintf("%06d", code%1_000_000), spotifyTOTPVersion, nil
}
