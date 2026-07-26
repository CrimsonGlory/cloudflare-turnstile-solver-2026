window.addEventListener("message", (event) => {
    // Data must come from our own webpage.
    if (event.source != window || !event.data) return;

    // Forward the requested proxy to the background.
    if (event.data.type == "FORWARD_TO_BACKGROUND") {
        browser.runtime.sendMessage({
            action: "setup_proxy",
            proxy_details: event.data.proxy_details
        }).then((response) => {
            if (response && response.success) {
                // Return message to the page telling the client the proxy is ready.
                window.postMessage({ type: "PROXY_READY" }, "*");
            }
        }).catch((error) => {
            console.error("[Proxy Bridge] Failed to communicate with background:", error);
        });
    }
});