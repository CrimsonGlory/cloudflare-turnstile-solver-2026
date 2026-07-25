window.addEventListener("message", (event) => {
    // Data must come from our own webpage.
    if (event.source !== window || !event.data) return;

    // Forward the requested proxy to the background.
    if (event.data.type === "FORWARD_SETUP_PROXY") {
        chrome.runtime.sendMessage({
            action: "setup_proxy",
            proxy_details: event.data.proxy_details
        }, (response) => {
            if (response && response.success) {
                // Return message to the page telling the client the proxy is ready--async unlocks once received,
                // giving us a mock lock/await.
                window.postMessage({ type: "PROXY_READY" }, "*");
            }
        });
    }
});