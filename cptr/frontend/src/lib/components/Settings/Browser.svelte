<script lang="ts">
	import { toast } from 'svelte-sonner';
	import ToggleSwitch from '../common/ToggleSwitch.svelte';
	import Spinner from '../common/Spinner.svelte';
	import { onMount } from 'svelte';
	import { getAdminConfig, updateConfig } from '$lib/apis/admin';
	import { t } from '$lib/i18n';

	let loading = $state(true);
	let saving = $state(false);
	let testing = $state(false);
	let testResult = $state<{ ok: boolean; message: string } | null>(null);

	// Config state
	let enabled = $state(false);
	let provider = $state<'local' | 'firecrawl' | 'browser_use'>('local');
	let cdpUrl = $state('http://localhost:9222');
	let autoLaunch = $state(true);
	let sessionTimeout = $state(10);
	let firecrawlApiKey = $state('');
	let firecrawlBaseUrl = $state('https://api.firecrawl.dev');
	let browserUseApiKey = $state('');
	let browserUseBaseUrl = $state('https://api.browser-use.com');

	onMount(async () => {
		try {
			const config = await getAdminConfig();
			enabled = config['browser.enabled'] === true || config['browser.enabled'] === 'true';
			provider = (config['browser.provider'] as typeof provider) || 'local';
			cdpUrl = (config['browser.cdp_url'] as string) || 'http://localhost:9222';
			autoLaunch = config['browser.auto_launch'] !== false && config['browser.auto_launch'] !== 'false';
			sessionTimeout = Number(config['browser.session_timeout_minutes']) || 10;
			firecrawlApiKey = (config['browser.firecrawl_api_key'] as string) || '';
			firecrawlBaseUrl = (config['browser.firecrawl_base_url'] as string) || 'https://api.firecrawl.dev';
			browserUseApiKey = (config['browser.browser_use_api_key'] as string) || '';
			browserUseBaseUrl = (config['browser.browser_use_base_url'] as string) || 'https://api.browser-use.com';
		} catch {}
		loading = false;
	});

	async function save() {
		saving = true;
		try {
			await updateConfig({
				'browser.enabled': enabled,
				'browser.provider': provider,
				'browser.cdp_url': cdpUrl,
				'browser.auto_launch': autoLaunch,
				'browser.session_timeout_minutes': sessionTimeout,
				'browser.firecrawl_api_key': firecrawlApiKey,
				'browser.firecrawl_base_url': firecrawlBaseUrl,
				'browser.browser_use_api_key': browserUseApiKey,
				'browser.browser_use_base_url': browserUseBaseUrl
			});
			toast.success($t('settings.saved'));
		} catch {
			toast.error('Failed to save browser settings');
		} finally {
			saving = false;
		}
	}

	async function testConnection() {
		testing = true;
		testResult = null;
		try {
			const resp = await fetch(`${cdpUrl}/json/version`);
			if (resp.ok) {
				const data = await resp.json();
				testResult = { ok: true, message: data.Browser || 'Connected' };
			} else {
				testResult = { ok: false, message: `HTTP ${resp.status}` };
			}
		} catch {
			testResult = { ok: false, message: 'Could not connect' };
		} finally {
			testing = false;
		}
	}
</script>

<div class="flex flex-col min-h-full">
	<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-4">Browser</h2>

	{#if loading}
		<div class="flex justify-center py-8"><Spinner size={16} /></div>
	{:else}
		<!-- Enable -->
		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">Enable</h3>

		<div class="flex flex-col gap-2.5">
			<label class="flex items-center justify-between cursor-pointer">
				<span class="text-xs text-gray-600 dark:text-gray-400">Browser tools</span>
				<ToggleSwitch value={enabled} onchange={(v) => { enabled = v; }} />
			</label>
			<p class="text-[11px] text-gray-400 dark:text-gray-600 -mt-1">
				Give the AI access to a web browser for navigating pages, clicking elements, and taking screenshots.
			</p>
		</div>

		{#if enabled}
			<!-- Provider -->
			<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">Provider</h3>

			<div class="flex gap-1">
				{#each [
					{ value: 'local' as const, label: 'Local CDP' },
					{ value: 'firecrawl' as const, label: 'Firecrawl' },
					{ value: 'browser_use' as const, label: 'Browser-Use' }
				] as opt}
					<button
						class="flex items-center gap-1.5 h-7 px-2.5 rounded-lg text-xs transition-colors duration-100
						{provider === opt.value
							? 'bg-gray-200/50 dark:bg-white/8 text-gray-900 dark:text-white font-medium'
							: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
						onclick={() => { provider = opt.value; }}
					>
						{opt.label}
					</button>
				{/each}
			</div>
			<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-1">
				{#if provider === 'local'}
					Connects to Chrome via DevTools Protocol. Full interactive browsing with clicking, typing, and screenshots.
				{:else if provider === 'firecrawl'}
					Cloud API that converts web pages to markdown. Fast extraction, no interactive browsing.
				{:else}
					Cloud API for LLM-driven browser tasks. Describe what you need in natural language.
				{/if}
			</p>

			<!-- Local CDP settings -->
			{#if provider === 'local'}
				<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">Connection</h3>

				<div class="flex flex-col gap-2.5">
					<label class="flex items-center justify-between cursor-pointer">
						<div>
							<span class="text-xs text-gray-600 dark:text-gray-400">Auto-launch Chrome</span>
							<p class="text-[10px] text-gray-400 dark:text-gray-600">Start a headless Chrome if none is running</p>
						</div>
						<ToggleSwitch value={autoLaunch} onchange={(v) => { autoLaunch = v; }} />
					</label>

					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="cdp-url">CDP URL</label>
						<div class="flex gap-1.5 mt-1">
							<input
								id="cdp-url"
								type="text"
								bind:value={cdpUrl}
								placeholder="http://localhost:9222"
								class="flex-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
							/>
							<button
								class="h-7 px-2.5 rounded-lg text-xs bg-gray-200/50 dark:bg-white/8 text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors disabled:opacity-50"
								onclick={() => testConnection()}
								disabled={testing}
							>
								{testing ? '...' : 'Test'}
							</button>
						</div>
						{#if testResult}
							<p class="text-[11px] mt-1 {testResult.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-500'}">
								{testResult.message}
							</p>
						{/if}
					</div>

					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="session-timeout">Session timeout</label>
						<div class="flex items-center gap-1.5 mt-1">
							<input
								id="session-timeout"
								type="number"
								bind:value={sessionTimeout}
								min="1"
								max="120"
								class="w-16 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
							/>
							<span class="text-[11px] text-gray-400 dark:text-gray-600">minutes</span>
						</div>
					</div>
				</div>
			{/if}

			<!-- Firecrawl settings -->
			{#if provider === 'firecrawl'}
				<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">Firecrawl</h3>

				<div class="flex flex-col gap-2.5">
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="fc-key">API Key</label>
						<input
							id="fc-key"
							type="password"
							bind:value={firecrawlApiKey}
							placeholder="fc-..."
							class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						/>
					</div>
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="fc-url">Base URL</label>
						<input
							id="fc-url"
							type="text"
							bind:value={firecrawlBaseUrl}
							placeholder="https://api.firecrawl.dev"
							class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						/>
						<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-1">Change for self-hosted Firecrawl instances</p>
					</div>
				</div>
			{/if}

			<!-- Browser-Use settings -->
			{#if provider === 'browser_use'}
				<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">Browser-Use</h3>

				<div class="flex flex-col gap-2.5">
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="bu-key">API Key</label>
						<input
							id="bu-key"
							type="password"
							bind:value={browserUseApiKey}
							placeholder="bu-..."
							class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						/>
					</div>
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400" for="bu-url">Base URL</label>
						<input
							id="bu-url"
							type="text"
							bind:value={browserUseBaseUrl}
							placeholder="https://api.browser-use.com"
							class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						/>
					</div>
				</div>
			{/if}
		{/if}

		<!-- Save -->
		<div class="mt-auto pt-6 flex justify-end">
			<button
				class="text-[13px] text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors duration-100 disabled:opacity-50"
				onclick={() => save()}
				disabled={saving}
			>{$t('settings.save')}</button>
		</div>
	{/if}
</div>
