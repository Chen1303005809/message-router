(() => {
      const configEndpoint = window.kefuUrl('/api/wecom/agent-js-sdk-config');
      const sdkScripts = [
        'https://res.wx.qq.com/open/js/jweixin-1.2.0.js',
        'https://open.work.weixin.qq.com/wwopen/js/jwxwork-1.0.0.js'
      ];
      const loadScript = (src) => new Promise((resolve, reject) => {
        if (document.querySelector(`script[src="${src}"]`)) {
          if (window.wx) { resolve(); return; }
        }
        const script = document.createElement('script');
        script.src = src;
        script.async = true;
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
      });
      const invoke = (method) => new Promise((resolve) => {
        let finished = false;
        const timer = window.setTimeout(() => finish(null), 4000);
        const finish = (value) => {
          if (finished) return;
          finished = true;
          window.clearTimeout(timer);
          resolve(value);
        };
        try {
          window.wx.invoke(method, {}, (result) => finish(result || null));
        } catch (_) {
          finish(null);
        }
      });
      window.getWeComCustomerGroupId = () => {
        const emptyResult = (status, error = '') => ({ chatId: '', status, error });
        const sanitizeSdkError = (value) => typeof value === 'string'
          ? value.replace(/[^A-Za-z0-9_.:-]+/g, '_').slice(0, 100)
          : '';
        const sdkErrorDetail = (result, method) => {
          const value = result && (result.err_msg || result.errMsg || result.errmsg);
          if (typeof value === 'string' && value) return sanitizeSdkError(value);
          const keys = result && typeof result === 'object'
            ? Object.keys(result).sort().slice(0, 8).join('_')
            : 'no_result_object';
          return `sdk_error_field_empty_keys_${sanitizeSdkError(keys) || 'none'}_${method}`;
        };
        const sdkCallSucceeded = (result, method) => Boolean(result && [
          result.err_msg,
          result.errMsg,
          result.errmsg
        ].includes(`${method}:ok`));
        const reportResult = (result) => {
          window.wecomCustomerGroupLookupStatus = result.status;
          window.wecomCustomerGroupLookupError = result.error || '';
          try {
            if (window.console && typeof window.console.info === 'function') {
              window.console.info('[wecom-group-context]', result.status, result.error || '');
            }
          } catch (_) {}
          return result.chatId;
        };
        if (!/wxwork/i.test(navigator.userAgent || '')) {
          return Promise.resolve(reportResult(emptyResult('not_wecom_client')));
        }
        const lookup = async () => {
          try {
            try {
              for (const src of sdkScripts) await loadScript(src);
            } catch (_) {
              return emptyResult('sdk_script_load_failed');
            }
            if (!window.wx || typeof window.wx.agentConfig !== 'function') {
              return emptyResult('sdk_api_unavailable');
            }
            const pageUrl = window.location.href.split('#', 1)[0];
            let response;
            try {
              response = await fetch(
                `${configEndpoint}?url=${encodeURIComponent(pageUrl)}`,
                { credentials: 'same-origin' }
              );
            } catch (_) {
              return emptyResult('js_sdk_config_fetch_failed');
            }
            if (!response.ok) return emptyResult('js_sdk_config_http_error');
            let config;
            try {
              config = await response.json();
            } catch (_) {
              return emptyResult('js_sdk_config_invalid');
            }
            if (!config || !config.corpid || !config.agentid || !config.signature) {
              return emptyResult('js_sdk_config_invalid');
            }
            let contextStatus = 'agent_config_timeout';
            let contextError = '';
            const context = await new Promise((resolve) => {
              let finished = false;
              const timer = window.setTimeout(() => finish(null), 5000);
              const finish = (value) => {
                if (finished) return;
                finished = true;
                window.clearTimeout(timer);
                resolve(value);
              };
              try {
                window.wx.agentConfig({
                  corpid: config.corpid,
                  agentid: config.agentid,
                  timestamp: config.timestamp,
                  nonceStr: config.nonceStr,
                  signature: config.signature,
                  jsApiList: ['getContext', 'getCurExternalChat'],
                  success: async () => {
                    const result = await invoke('getContext');
                    contextStatus = sdkCallSucceeded(result, 'getContext')
                      ? 'context_ready'
                      : 'get_context_failed';
                    if (contextStatus === 'get_context_failed') {
                      contextError = sdkErrorDetail(result, 'getContext');
                    }
                    finish(sdkCallSucceeded(result, 'getContext') ? result : null);
                  },
                  fail: (error) => {
                    contextStatus = 'agent_config_failed';
                    contextError = sdkErrorDetail(error, 'agentConfig');
                    finish(null);
                  }
                });
              } catch (_) {
                contextStatus = 'agent_config_failed';
                finish(null);
              }
            });
            if (!context) return emptyResult(contextStatus, contextError);
            if (context.entry !== 'group_chat_tools') {
              return emptyResult('not_group_chat_tools');
            }
            const group = await invoke('getCurExternalChat');
            if (!group) return emptyResult('get_cur_external_chat_timeout');
            if (!sdkCallSucceeded(group, 'getCurExternalChat')) {
              return emptyResult(
                'get_cur_external_chat_failed',
                sdkErrorDetail(group, 'getCurExternalChat')
              );
            }
            if (typeof group.chatId !== 'string' || !group.chatId.trim()) {
              return emptyResult('chat_id_missing');
            }
            return { chatId: group.chatId, status: 'success' };
          } catch (_) {
            return emptyResult('lookup_exception');
          }
        };
        return Promise.race([
          lookup(),
          new Promise((resolve) => window.setTimeout(
            () => resolve(emptyResult('lookup_timeout')),
            9000
          ))
        ]).then(reportResult);
      };
    })();
