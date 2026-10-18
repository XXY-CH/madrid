//! madrid Rust client — single dependency (ureq).
//!
//! ```rust
//! use madrid::Madrid;
//! let im = Madrid::new("http://127.0.0.1:8787");
//! im.send("chat-+15551234567", "hello **world**", "markdown").unwrap();
//! ```

use std::collections::HashMap;
use ureq::json;

pub struct Madrid {
    base: String,
    agent: ureq::Agent,
}

impl Madrid {
    pub fn new(base: &str) -> Self {
        Self {
            base: base.trim_end_matches('/').to_string(),
            agent: ureq::AgentBuilder::new()
                .timeout(std::time::Duration::from_secs(30))
                .build(),
        }
    }

    fn get(&self, path: &str) -> Result<serde_json::Value, String> {
        self.agent
            .get(&format!("{}{}", self.base, path))
            .call()
            .map_err(|e| e.to_string())?
            .into_json()
            .map_err(|e| e.to_string())
    }

    fn post(&self, path: &str, body: serde_json::Value) -> Result<serde_json::Value, String> {
        self.agent
            .post(&format!("{}{}", self.base, path))
            .send_json(body)
            .map_err(|e| e.to_string())?
            .into_json()
            .map_err(|e| e.to_string())
    }

    pub fn health(&self) -> Result<serde_json::Value, String> {
        self.get("/health")
    }

    pub fn list_chats(&self) -> Result<serde_json::Value, String> {
        self.get("/chats")
    }

    pub fn send(&self, chat_id: &str, text: &str, format: &str) -> Result<serde_json::Value, String> {
        self.post("/send", json!({
            "chat_id": chat_id, "text": text, "format": format
        }))
    }

    pub fn edit(&self, guid: &str, text: &str) -> Result<serde_json::Value, String> {
        self.post("/edit", json!({"guid": guid, "text": text}))
    }

    pub fn retract(&self, guid: &str) -> Result<serde_json::Value, String> {
        self.post("/retract", json!({"guid": guid}))
    }

    pub fn tapback(&self, guid: &str, kind: &str) -> Result<serde_json::Value, String> {
        self.post("/tapback", json!({"guid": guid, "kind": kind}))
    }

    pub fn typewriter(&self, handle: &str, frames: &[&str], interval: f64) -> Result<serde_json::Value, String> {
        self.post("/typewriter", json!({
            "handle": handle, "frames": frames, "interval": interval
        }))
    }

    pub fn real_chats(&self, limit: i32) -> Result<serde_json::Value, String> {
        self.get(&format!("/real/chats?limit={}", limit))
    }

    pub fn stats(&self) -> Result<serde_json::Value, String> {
        self.get("/stats")
    }
}
