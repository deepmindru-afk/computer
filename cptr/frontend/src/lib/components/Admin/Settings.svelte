<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { onMount } from 'svelte';
	import { getAdminConfig, updateConfig as apiUpdateConfig } from '$lib/apis/admin';
	import { t } from '$lib/i18n';
	import ToggleSwitch from '$lib/components/common/ToggleSwitch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	let config = $state<Record<string, any>>({});
	let loading = $state(true);
	let saving = $state(false);

	// Local state for key inputs (save on blur, not every keystroke)
	let exaKey = $state('');
	let tavilyKey = $state('');
	let braveKey = $state('');
	let perplexityKey = $state('');
	let ccKey = $state('');
	let ccBaseUrl = $state('');
	let ccModel = $state('');

	async function loadConfig() {
		try {
			config = await getAdminConfig();
			exaKey = config['web.exa_api_key'] || '';
			tavilyKey = config['web.tavily_api_key'] || '';
			braveKey = config['web.brave_api_key'] || '';
			perplexityKey = config['web.perplexity_api_key'] || '';
			ccKey = config['web.chat_completions_api_key'] || '';
			ccBaseUrl = config['web.chat_completions_base_url'] || '';
			ccModel = config['web.chat_completions_model'] || '';
		} catch {
			toast.error($t('admin.failedToLoadConfig'));
		} finally {
			loading = false;
		}
	}

	async function updateConfig(key: string, value: any) {
		saving = true;
		try {
			await apiUpdateConfig({ [key]: value });
			config[key] = value;
			toast.success($t('settings.saved'));
		} catch {
			toast.error($t('admin.failedToSave'));
		} finally {
			saving = false;
		}
	}

	async function saveKey(key: string, value: string) {
		if (value !== (config[key] || '')) {
			await updateConfig(key, value);
		}
	}

	onMount(loadConfig);
</script>

<div class="flex flex-col min-h-full">
	<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-4">{$t('admin.settings')}</h2>

	{#if loading}
		<div class="flex justify-center py-8">
			<Spinner size={16} />
		</div>
	{:else}
		<!-- Auth settings -->
		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">{$t('admin.authentication')}</h3>

		<div class="flex items-center justify-between h-8">
			<span class="text-[13px] text-gray-700 dark:text-gray-300">{$t('admin.allowSignUp')}</span>
			<ToggleSwitch
				value={config['auth.signup_enabled']}
				onchange={(v) => updateConfig('auth.signup_enabled', v)}
				disabled={saving}
			/>
		</div>
		<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-0.5">
			{config['auth.signup_enabled'] ? $t('admin.signUpEnabled') : $t('admin.signUpDisabled')}
		</p>

		<!-- Web settings -->
		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-6">{$t('admin.web')}</h3>

		<!-- Enable/disable toggle -->
		<div class="flex items-center justify-between h-8">
			<span class="text-[13px] text-gray-700 dark:text-gray-300">{$t('admin.webEnabled')}</span>
			<ToggleSwitch
				value={config['web.enabled'] !== false}
				onchange={() => updateConfig('web.enabled', config['web.enabled'] === false)}
				disabled={saving}
			/>
		</div>
		<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-0.5">
			{config['web.enabled'] !== false ? $t('admin.webEnabledHint') : $t('admin.webDisabledHint')}
		</p>

		{#if config['web.enabled'] !== false}
			<!-- Provider selector -->
			<div class="flex items-center justify-between h-8 mt-3">
				<span class="text-[13px] text-gray-700 dark:text-gray-300"
					>{$t('admin.webSearchProvider')}</span
				>
				<select
					class="text-[13px] bg-transparent text-gray-700 dark:text-gray-300 text-right outline-none cursor-pointer"
					value={config['web.search_provider'] || 'auto'}
					onchange={(e) =>
						updateConfig('web.search_provider', (e.target as HTMLSelectElement).value)}
					disabled={saving}
				>
					<option value="auto">{$t('admin.webProviderAuto')}</option>
					<option value="exa">Exa</option>
					<option value="tavily">Tavily</option>
					<option value="brave">Brave</option>
					<option value="perplexity">Perplexity</option>
					<option value="duckduckgo">DuckDuckGo</option>
					<option value="chat_completions">{$t('admin.webChatCompletions')}</option>
				</select>
			</div>

			<!-- Conditionally mounted API key fields -->
			{@const provider = config['web.search_provider'] || 'auto'}

			{#if provider === 'auto'}
				<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-2">{$t('admin.webAutoHint')}</p>
			{:else if provider === 'exa'}
				<div class="mt-3">
					<label class="text-xs text-gray-600 dark:text-gray-400 mb-1"
						>{$t('admin.webExaKey')}</label
					>
					<input
						type="password"
						class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						placeholder="exa-..."
						bind:value={exaKey}
						onblur={() => saveKey('web.exa_api_key', exaKey)}
						disabled={saving}
					/>
					<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-0.5">
						{$t('admin.webExaHint')}
					</p>
				</div>
			{:else if provider === 'tavily'}
				<div class="mt-3">
					<label class="text-xs text-gray-600 dark:text-gray-400 mb-1"
						>{$t('admin.webTavilyKey')}</label
					>
					<input
						type="password"
						class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						placeholder="tvly-..."
						bind:value={tavilyKey}
						onblur={() => saveKey('web.tavily_api_key', tavilyKey)}
						disabled={saving}
					/>
					<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-0.5">
						{$t('admin.webTavilyHint')}
					</p>
				</div>
			{:else if provider === 'brave'}
				<div class="mt-3">
					<label class="text-xs text-gray-600 dark:text-gray-400 mb-1"
						>{$t('admin.webBraveKey')}</label
					>
					<input
						type="password"
						class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						placeholder="BSA..."
						bind:value={braveKey}
						onblur={() => saveKey('web.brave_api_key', braveKey)}
						disabled={saving}
					/>
					<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-0.5">
						{$t('admin.webBraveHint')}
					</p>
				</div>
			{:else if provider === 'duckduckgo'}
				<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-2">
					{$t('admin.webDuckDuckGoNote')}
				</p>
			{:else if provider === 'perplexity'}
				<div class="mt-3">
					<label class="text-xs text-gray-600 dark:text-gray-400 mb-1"
						>{$t('admin.webPerplexityKey')}</label
					>
					<input
						type="password"
						class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
						placeholder="pplx-..."
						bind:value={perplexityKey}
						onblur={() => saveKey('web.perplexity_api_key', perplexityKey)}
						disabled={saving}
					/>
					<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-0.5">
						{$t('admin.webPerplexityHint')}
					</p>
				</div>
			{:else if provider === 'chat_completions'}
				<div class="mt-3 space-y-3">
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400 mb-1"
							>{$t('admin.webCcBaseUrl')}</label
						>
						<input
							type="text"
							class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
							placeholder="https://api.perplexity.ai/v1"
							bind:value={ccBaseUrl}
							onblur={() => saveKey('web.chat_completions_base_url', ccBaseUrl)}
							disabled={saving}
						/>
					</div>
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400 mb-1"
							>{$t('admin.webCcKey')}</label
						>
						<input
							type="password"
							class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
							placeholder="sk-..."
							bind:value={ccKey}
							onblur={() => saveKey('web.chat_completions_api_key', ccKey)}
							disabled={saving}
						/>
					</div>
					<div>
						<label class="text-xs text-gray-600 dark:text-gray-400 mb-1"
							>{$t('admin.webCcModel')}</label
						>
						<input
							type="text"
							class="w-full mt-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
							placeholder="sonar-pro"
							bind:value={ccModel}
							onblur={() => saveKey('web.chat_completions_model', ccModel)}
							disabled={saving}
						/>
					</div>
					<p class="text-[11px] text-gray-400 dark:text-gray-600">
						{$t('admin.webCcHint')}
					</p>
				</div>
			{/if}
		{/if}

		<div class="mt-auto pt-6 flex justify-end">
			<button
				class="text-[13px] text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors duration-100"
				onclick={() => toast.success($t('settings.saved'))}>{$t('settings.save')}</button
			>
		</div>
	{/if}
</div>
