use {
    crate::model::SendRecord,
    anyhow::Result,
    serde::Serialize,
    std::{
        fs::{self, OpenOptions},
        io::{BufRead, BufReader, Write},
        path::Path,
        time::{SystemTime, UNIX_EPOCH},
    },
};

pub fn unix_ns() -> i128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as i128)
        .unwrap_or(0)
}

pub fn new_session_id(live: bool) -> String {
    let t = chrono::Utc::now().format("%Y%m%dT%H%M%SZ");
    format!("{t}-{}", if live { "live" } else { "dry" })
}

pub fn host_of(url: &str) -> String {
    url.split("://")
        .nth(1)
        .unwrap_or(url)
        .split(['/', '?'])
        .next()
        .unwrap_or("")
        .to_string()
}

pub fn write_json<T: Serialize + ?Sized>(path: &Path, v: &T) -> Result<()> {
    fs::write(path, serde_json::to_vec_pretty(v)?)?;
    Ok(())
}

pub fn append_jsonl<T: Serialize>(path: &Path, rows: &[T]) -> Result<()> {
    let mut f = OpenOptions::new().create(true).append(true).open(path)?;
    for r in rows {
        serde_json::to_writer(&mut f, r)?;
        f.write_all(b"\n")?;
    }
    Ok(())
}

pub fn read_jsonl<T: serde::de::DeserializeOwned>(path: &Path) -> Result<Vec<T>> {
    let f = fs::File::open(path)?;
    let mut out = Vec::new();
    for line in BufReader::new(f).lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        out.push(serde_json::from_str(&line)?);
    }
    Ok(out)
}

/// Sum of the maximum fees of every probe that a live session actually handed to a sender.
/// Used to carry the spend cap across sessions.
pub fn committed_live_spend(out_dir: &Path) -> Result<u64> {
    let mut total = 0u64;
    let Ok(entries) = fs::read_dir(out_dir) else {
        return Ok(0);
    };
    for e in entries.flatten() {
        let p = e.path().join("sends.jsonl");
        if !p.exists() {
            continue;
        }
        let rows: Vec<SendRecord> = read_jsonl(&p)?;
        total += rows
            .iter()
            .filter(|r| !r.dry_run && r.send_error.is_none())
            .map(|r| r.max_fee_lamports)
            .sum::<u64>();
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_strips_key() {
        assert_eq!(
            host_of("https://mainnet.helius-rpc.com/?api-key=SECRET"),
            "mainnet.helius-rpc.com"
        );
        assert_eq!(host_of("http://localhost:8899"), "localhost:8899");
    }
}
