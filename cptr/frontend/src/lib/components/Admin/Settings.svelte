<script lang="ts">
	import { toast } from 'svelte-sonner';
	import { onMount } from 'svelte';
	import { getAdminConfig, updateConfig as apiUpdateConfig } from '$lib/apis/admin';
	import { t } from '$lib/i18n';
	import ToggleSwitch from '$lib/components/ToggleSwitch.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';

	let config = $state<Record<string, any>>({});
	let loading = $state(true);
	let saving = $state(false);

	// Local state for key inputs (save on blur, not every keystroke)
	let exaKey = $state('');
	let tavilyKey = $state('');
	let braveKey = $state('');

	async function loadConfig() {
		try {
			config = await getAdminConfig();
			exaKey = config['web.exa_api_key'] || '';
			tavilyKey = config['web.tavily_api_key'] || '';
			braveKey = config['web.brave_api_key'] || '';
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
					<option value="duckduckgo">DuckDuckGo</option>
				</select>
			</div>

			<!-- Conditionally mounted API key fields -->
			{@const provider = config['web.search_provider'] || 'auto'}

			{#if provider === 'auto'}
				<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-2">{$t('admin.webAutoHint')}</p>
			{:else if provider === 'exa'}
				<div class="mt-3">
					<label class="block text-[13px] text-gray-700 dark:text-gray-300 mb-1"
						>{$t('admin.webExaKey')}</label
					>
					<input
						type="password"
						class="w-full text-[13px] bg-gray-50 dark:bg-white/4 border border-gray-200 dark:border-white/8 rounded-lg px-2.5 py-1.5 outline-none text-gray-700 dark:text-gray-300 placeholder:text-gray-400 dark:placeholder:text-gray-600"
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
					<label class="block text-[13px] text-gray-700 dark:text-gray-300 mb-1"
						>{$t('admin.webTavilyKey')}</label
					>
					<input
						type="password"
						class="w-full text-[13px] bg-gray-50 dark:bg-white/4 border border-gray-200 dark:border-white/8 rounded-lg px-2.5 py-1.5 outline-none text-gray-700 dark:text-gray-300 placeholder:text-gray-400 dark:placeholder:text-gray-600"
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
					<label class="block text-[13px] text-gray-700 dark:text-gray-300 mb-1"
						>{$t('admin.webBraveKey')}</label
					>
					<input
						type="password"
						class="w-full text-[13px] bg-gray-50 dark:bg-white/4 border border-gray-200 dark:border-white/8 rounded-lg px-2.5 py-1.5 outline-none text-gray-700 dark:text-gray-300 placeholder:text-gray-400 dark:placeholder:text-gray-600"
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
