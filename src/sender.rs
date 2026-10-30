//! Two delivery paths.
//!
//! * RPC: `sendTransaction` with `skipPreflight` and `maxRetries = 0`, so the provider
//!   forwards each transaction once and does not re-send it later in a different order.
//! * TPU: QUIC straight to the leader's `tpuQuic` address with an unstaked identity, using
//!   Agave's `tpu-client-next`. The leader list is pinned to the window's leader.

use {
    anyhow::{anyhow, Result},
    async_trait::async_trait,
    solana_rpc_client::nonblocking::rpc_client::RpcClient,
    solana_rpc_client_api::config::RpcSendTransactionConfig,
    solana_tpu_client_next::{leader_updater::LeaderUpdater, Client, ClientBuilder, TransactionSender},
    solana_transaction::Transaction,
    std::{
        net::{SocketAddr, UdpSocket},
        num::NonZeroUsize,
        sync::{Arc, Mutex},
    },
};

pub fn wire(tx: &Transaction) -> Vec<u8> {
    bincode::serialize(tx).expect("transaction serializes")
}

pub async fn send_rpc(rpc: &RpcClient, tx: &Transaction) -> Result<()> {
    let cfg = RpcSendTransactionConfig {
        skip_preflight: true,
        max_retries: Some(0),
        ..RpcSendTransactionConfig::default()
    };
    rpc.send_transaction_with_config(tx, cfg).await.map(|_| ()).map_err(|e| anyhow!("{e}"))
}

/// Leader updater that always returns whatever address the probe pinned last.
struct PinnedTarget {
    target: Arc<Mutex<Vec<SocketAddr>>>,
}

#[async_trait]
impl LeaderUpdater for PinnedTarget {
    fn next_leaders(&mut self, _lookahead_leaders: usize) -> Vec<SocketAddr> {
        self.target.lock().unwrap().clone()
    }

    async fn stop(&mut self) {}
}

pub struct TpuPath {
    target: Arc<Mutex<Vec<SocketAddr>>>,
    sender: TransactionSender,
    client: Option<Client>,
}

impl TpuPath {
    /// Must be called inside a Tokio runtime.
    pub fn start() -> Result<Self> {
        let target = Arc::new(Mutex::new(Vec::new()));
        let socket = UdpSocket::bind("0.0.0.0:0")?;
        let (sender, client) = ClientBuilder::new(Box::new(PinnedTarget { target: target.clone() }))
            .bind_socket(socket)
            .leader_send_fanout(1)
            .max_cache_size(NonZeroUsize::new(32).unwrap())
            .max_reconnect_attempts(2)
            .build()
            .map_err(|e| anyhow!("tpu client: {e:?}"))?;
        Ok(Self { target, sender, client: Some(client) })
    }

    pub fn pin(&self, addr: SocketAddr) {
        *self.target.lock().unwrap() = vec![addr];
    }

    pub async fn send(&self, tx: &Transaction) -> Result<()> {
        self.sender
            .send_transactions_in_batch(vec![wire(tx)])
            .await
            .map_err(|e| anyhow!("tpu send: {e:?}"))
    }

    pub async fn shutdown(mut self) {
        if let Some(c) = self.client.take() {
            let _ = c.shutdown().await;
        }
    }
}
