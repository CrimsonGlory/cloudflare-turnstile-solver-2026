window.addEventListener("message", (event) => {
    // Data must come from our own webpage.
    if (event.source != window || !event.data) return;

    if (event.data.type == "SET_TAB_PROXY") {
        
        // Spoof JS fields by injecting a script overriding the fields into the page.
        if (event.data.js_API_spoof_fields) {
            let script = document.createElement("script");
            script.textContent = `
                (function() {
                    let fields = ${JSON.stringify(event.data.js_API_spoof_fields)};
                    
                    for (let key in fields) {
                        let val = fields[key];
                        
                        // Parse strings into primitive types if applicable.
                        if (val === "true") {
                            val = true;
                        } else if (val === "false") {
                            val = false;
                        } else if (val.trim() !== "" && !isNaN(Number(val))) {
                            val = Number(val);
                        }
                        
                        let parts = key.split(".");
                        
                        // Global parent obj.
                        let obj = window;
                        
                        let prop = parts.pop();
                        
                        // Go through each part/term (e.g. part.part.part.part),
                        // until we traverse all the way to the end. If for some reason,
                        // what you've set isn't defined, an empty object {} is added so that no error occurs.
                        for (let i = 0; i < parts.length; i++) {
                            if (!obj[parts[i]]) obj[parts[i]] = {}; 
                            obj = obj[parts[i]];
                        }
                        
                        // Create a hidden global symbol acting as the data store for this specific field.
                        // Using a symbol allows our script to re-edit it.
                        let sym = Symbol.for("__proxy_spoof_" + key);
                        
                        // Check if we have already locked this property on a previous proxy setup.
                        if (!Object.getOwnPropertySymbols(obj).includes(sym)) {
                            obj[sym] = val;
                            
                            // For our target value, modify its properties to simply force get to return it, 
                            // and force set to do nothing so it can't be edited.
                            Object.defineProperty(obj, prop, {
                                get: () => obj[sym],
                                set: () => {}, 
                                configurable: false
                            });
                        } else {
                            // The property has alread been locked, so resetting it directly will not work.
                            // This is why we stored the object in a Symbol reference. We can directly modify this reference instead.
                            obj[sym] = val; 
                        }
                    }
                })();
            `;
            
            // Inject and then immediately remove the script tag after execution is complete.
            document.documentElement.appendChild(script);
            script.remove();
        }

        // Forward the requested proxy to the background.
        browser.runtime.sendMessage({
            action: "setup_proxy",
            proxy_details: event.data.proxy_details
        }).then((response) => {
            if (response && response.success) {
                // Return message to the page telling the client the proxy is ready--async unlocks once received,
                // giving us a mock lock/await.
                window.postMessage({ type: "PROXY_READY" }, "*");
            }
        }).catch((err) => {
            console.error("Extension bridge error:", err);
        });
    }
});