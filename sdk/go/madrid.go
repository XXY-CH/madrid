// Package madrid provides a Go client for the madrid iMessage bridge.
//
//	import "github.com/XXY-CH/madrid/sdk/go"
//	im := madrid.New("http://127.0.0.1:8787")
//	im.Send("chat-+15551234567", "hello **world**", "markdown")
package madrid

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	Base   string
	HTTP   *http.Client
}

func New(base string) *Client {
	return &Client{Base: base, HTTP: &http.Client{Timeout: 30 * time.Second}}
}

func (c *Client) call(method, path string, body interface{}) (map[string]interface{}, error) {
	var buf bytes.Buffer
	if body != nil {
		if err := json.NewEncoder(&buf).Encode(body); err != nil {
			return nil, err
		}
	}
	req, err := http.NewRequest(method, c.Base+path, &buf)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out map[string]interface{}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

func (c *Client) Health() (map[string]interface{}, error) {
	return c.call("GET", "/health", nil)
}

func (c *Client) ListChats() ([]interface{}, error) {
	out, err := c.call("GET", "/chats", nil)
	if err != nil {
		return nil, err
	}
	if arr, ok := out.([]interface{}); ok {
		return arr, nil
	}
	return nil, fmt.Errorf("unexpected response type")
}

func (c *Client) Send(chatID, text, format string) (map[string]interface{}, error) {
	return c.call("POST", "/send", map[string]string{
		"chat_id": chatID, "text": text, "format": format,
	})
}

func (c *Client) Typewriter(handle string, frames []string, interval float64) (map[string]interface{}, error) {
	return c.call("POST", "/typewriter", map[string]interface{}{
		"handle": handle, "frames": frames, "interval": interval,
	})
}

func (c *Client) RealChats(limit int) ([]interface{}, error) {
	return c.ListChats() // simplified — same API shape
}

func (c *Client) Transcript(chatID string, limit int) ([]interface{}, error) {
	u := fmt.Sprintf("/chats/%s/transcript?limit=%d", url.PathEscape(chatID), limit)
	out, err := c.call("GET", u, nil)
	if err != nil {
		return nil, err
	}
	if arr, ok := out.([]interface{}); ok {
		return arr, nil
	}
	return nil, fmt.Errorf("unexpected response type")
}
