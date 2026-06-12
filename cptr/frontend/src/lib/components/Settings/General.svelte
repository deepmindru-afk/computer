<script lang="ts">
	import { toast } from 'svelte-sonner';
	import Icon from '../Icon.svelte';
	import { theme, streamingBehavior, showUpdateToastPref } from '$lib/stores';
	import type { Theme, StreamingBehavior } from '$lib/stores';
	import { t, locale, changeLocale, supportedLocales } from '$lib/i18n';
	import { notificationsEnabled, notificationSound } from '$lib/stores/chat';
	import { fetchJSON } from '$lib/apis';
	import { session } from '$lib/session';
	import ToggleSwitch from '../common/ToggleSwitch.svelte';
	import { onMount } from 'svelte';

	function setTheme(v: Theme) {
		theme.set(v);
	}

	// ── Webhook URL ─────────────────────────────────────────────
	let webhookUrl = $state('');
	let webhookLoading = $state(false);

	onMount(async () => {
		try {
			const data = await fetchJSON<{ config: Record<string, any> }>('/api/admin/config/notifications');
			webhookUrl = data.config?.['notifications.webhook_url'] || '';
		} catch {}
	});

	async function saveWebhookUrl() {
		webhookLoading = true;
		try {
			await fetchJSON('/api/admin/config', {
				method: 'PUT',
				body: JSON.stringify({ config: { 'notifications.webhook_url': webhookUrl.trim() || null } })
			});
			toast.success($t('general.webhookUrlSaved'));
		} catch {
			toast.error($t('general.webhookUrlSaveFailed'));
		} finally {
			webhookLoading = false;
		}
	}

	async function toggleNotifications() {
		if (!$notificationsEnabled) {
			// Enabling: request permission
			if ('Notification' in window) {
				const permission = await Notification.requestPermission();
				if (permission === 'granted') {
					notificationsEnabled.set(true);
				} else {
					toast.error($t('general.notificationPermissionDenied'));
				}
			}
		} else {
			notificationsEnabled.set(false);
		}
	}
</script>

<div class="flex flex-col min-h-full">
	<h2 class="text-sm font-medium text-gray-900 dark:text-white mb-4">{$t('general.title')}</h2>

	<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2">{$t('general.theme')}</h3>
	<div class="flex gap-1">
		{#each [{ value: 'light' as Theme, label: $t('general.light'), icon: 'sun-light' }, { value: 'dark' as Theme, label: $t('general.dark'), icon: 'half-moon' }, { value: 'system' as Theme, label: $t('general.system'), icon: 'monitor' }] as opt}
			<button
				class="flex items-center gap-1.5 h-7 px-2.5 rounded-lg text-xs transition-colors duration-100
				{$theme === opt.value
					? 'bg-gray-200/50 dark:bg-white/8 text-gray-900 dark:text-white font-medium'
					: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
				onclick={() => setTheme(opt.value)}
			>
				<Icon name={opt.icon} size={13} />
				{opt.label}
			</button>
		{/each}
	</div>

	<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">{$t('general.language')}</h3>
	<select
		class="w-full max-w-[200px] bg-transparent text-[13px] text-gray-700 dark:text-gray-300 outline-none py-1 cursor-pointer"
		value={$locale}
		onchange={(e) => changeLocale((e.currentTarget as HTMLSelectElement).value)}
	>
		{#each supportedLocales as loc}
			<option value={loc.code}>{loc.label}</option>
		{/each}
	</select>

	<!-- Notifications -->
	<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">{$t('general.notifications')}</h3>

	<div class="flex flex-col gap-2.5">
		<!-- Browser notifications toggle -->
		<label class="flex items-center justify-between cursor-pointer">
			<span class="text-xs text-gray-600 dark:text-gray-400">{$t('general.browserNotifications')}</span>
			<ToggleSwitch value={$notificationsEnabled} onchange={() => toggleNotifications()} />
		</label>
		<p class="text-[11px] text-gray-400 dark:text-gray-600 -mt-1">
			{$t('general.browserNotificationsDesc')}
		</p>

		<!-- Sound toggle -->
		<label class="flex items-center justify-between cursor-pointer">
			<span class="text-xs text-gray-600 dark:text-gray-400">{$t('general.notificationSound')}</span>
			<ToggleSwitch value={$notificationSound} onchange={(v) => notificationSound.set(v)} />
		</label>

		<!-- Webhook URL -->
		<div class="mt-1">
			<label class="text-xs text-gray-600 dark:text-gray-400" for="webhook-url">
				{$t('general.webhookUrl')}
			</label>
			<div class="flex gap-1.5 mt-1">
				<input
					id="webhook-url"
					type="url"
					bind:value={webhookUrl}
					placeholder="https://hooks.slack.com/services/..."
					class="flex-1 h-7 px-2 rounded-lg text-xs bg-gray-100 dark:bg-white/6 text-gray-700 dark:text-gray-300 border border-gray-200 dark:border-white/8 outline-none focus:border-blue-400 dark:focus:border-blue-500 transition-colors"
				/>
				<button
					class="h-7 px-2.5 rounded-lg text-xs bg-gray-200/50 dark:bg-white/8 text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors disabled:opacity-50"
					onclick={saveWebhookUrl}
					disabled={webhookLoading}
				>
					{$t('settings.save')}
				</button>
			</div>
			<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-1">
				{$t('general.webhookUrlHint')}
			</p>
		</div>
	</div>

	{#if $session?.role === 'admin'}
		<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">{$t('general.updates')}</h3>
		<label class="flex items-center justify-between cursor-pointer">
			<span class="text-xs text-gray-600 dark:text-gray-400">{$t('general.updateNotifications')}</span>
			<ToggleSwitch value={$showUpdateToastPref} onchange={(v) => showUpdateToastPref.set(v)} />
		</label>
		<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-1">
			{$t('general.updateNotificationsDesc')}
		</p>
	{/if}

	<h3 class="text-xs text-gray-400 dark:text-gray-600 mb-2 mt-5">{$t('general.messageQueue')}</h3>
	<div class="flex gap-1">
		{#each [{ value: 'queue' as StreamingBehavior, label: $t('general.queue') }, { value: 'interrupt' as StreamingBehavior, label: $t('general.interrupt') }] as opt}
			<button
				class="flex items-center gap-1.5 h-7 px-2.5 rounded-lg text-xs transition-colors duration-100
				{$streamingBehavior === opt.value
					? 'bg-gray-200/50 dark:bg-white/8 text-gray-900 dark:text-white font-medium'
					: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
				onclick={() => streamingBehavior.set(opt.value)}
			>
				{opt.label}
			</button>
		{/each}
	</div>
	<p class="text-[11px] text-gray-400 dark:text-gray-600 mt-1">
		{$streamingBehavior === 'queue'
			? $t('general.queueDesc')
			: $t('general.interruptDesc')}
	</p>

	<div class="mt-auto pt-6 flex justify-end">
		<button
			class="text-[13px] text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white transition-colors duration-100"
			onclick={() => toast.success($t('settings.saved'))}>{$t('settings.save')}</button
		>
	</div>
</div>
