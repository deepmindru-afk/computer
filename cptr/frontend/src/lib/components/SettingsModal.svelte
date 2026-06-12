<script lang="ts">
	import Icon from './Icon.svelte';
	import Modal from './Modal.svelte';
	import General from './Settings/General.svelte';
	import Account from './Settings/Account.svelte';
	import Keyboard from './Settings/Keyboard.svelte';
	import Browser from './Settings/Browser.svelte';
	import About from './Settings/About.svelte';
	import Users from './Admin/Users.svelte';
	import Connections from './Admin/Connections.svelte';
	import Models from './Admin/Models.svelte';
	import Messaging from './Admin/Messaging.svelte';
	import Gateway from './Admin/Gateway.svelte';
	import AudioSettings from './Admin/AudioSettings.svelte';
	import AdminSettings from './Admin/Settings.svelte';
	import { session } from '$lib/session';
	import { t } from '$lib/i18n';

	type Tab =
		| 'general'
		| 'keyboard'
		| 'browser'
		| 'account'
		| 'about'
		| 'users'
		| 'connections'
		| 'models'
		| 'messaging'
		| 'gateway'
		| 'audio'
		| 'admin_settings';

	interface Props {
		onclose: () => void;
		initialTab?: Tab;
	}

	let { onclose, initialTab = 'general' }: Props = $props();

	let activeTab = $state<Tab>(initialTab);

	const isAdmin = $derived($session?.role === 'admin');

	const personalTabs: { id: Tab; label: string; icon: string }[] = $derived([
		{ id: 'general', label: $t('settings.general'), icon: 'settings' },
		{ id: 'keyboard', label: $t('settings.keyboard'), icon: 'terminal' },
		{ id: 'account', label: $t('settings.account'), icon: 'user' },
		{ id: 'about', label: $t('settings.about'), icon: 'info' }
	]);

	const adminTabs: { id: Tab; label: string; icon: string }[] = $derived([
		{ id: 'users', label: $t('admin.users'), icon: 'user' },
		{ id: 'connections', label: $t('admin.connections'), icon: 'plug' },
		{ id: 'models', label: $t('admin.models'), icon: 'cube' },
		{ id: 'messaging', label: $t('admin.messaging'), icon: 'chat-bubble' },
		{ id: 'gateway', label: $t('admin.gateway.tab'), icon: 'gateway' },
		{ id: 'audio', label: 'Audio', icon: 'microphone' },
		{ id: 'browser', label: 'Browser', icon: 'browser' },
		{ id: 'admin_settings', label: $t('settings.configuration'), icon: 'shield' }
	]);
</script>

<Modal
	{onclose}
	class="w-full max-w-3xl mx-4 md:mx-0 flex flex-col md:flex-row max-h-[85vh] md:h-[560px]"
>
	<nav
		class="shrink-0 min-w-0 overflow-x-auto md:overflow-x-visible scrollbar-hidden border-b md:border-b-0 md:border-r border-gray-200 dark:border-white/6 md:w-[180px]"
	>
		<div class="flex w-max min-w-full md:w-auto md:min-w-0 md:flex-col p-1 gap-px">
			<button
				class="flex items-center gap-1.5 h-7 px-2 md:w-full shrink-0 rounded-lg text-xs text-gray-400 dark:text-gray-600 hover:text-gray-700 dark:hover:text-gray-300 transition-colors duration-75 md:mb-1"
				onclick={onclose}
			>
				<Icon name="chevron-left" size={12} />
				<span>{$t('settings.back')}</span>
			</button>

			<!-- Personal -->
			{#each personalTabs as tab}
				<button
					class="flex items-center gap-1.5 h-7 px-2 md:w-full shrink-0 rounded-lg text-xs text-left transition-colors duration-75
						{activeTab === tab.id
						? 'font-medium text-gray-900 dark:text-white bg-gray-100 dark:bg-white/6'
						: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
					onclick={() => (activeTab = tab.id)}
				>
					<Icon name={tab.icon} size={14} />
					{tab.label}
				</button>
			{/each}

			<!-- Admin section -->
			{#if isAdmin}
				<span class="hidden md:block text-[10px] text-gray-400 dark:text-gray-600 px-2 mt-2 mb-0.5"
					>{$t('sidebar.admin')}</span
				>

				{#each adminTabs as tab}
					<button
						class="flex items-center gap-1.5 h-7 px-2 md:w-full shrink-0 rounded-lg text-xs text-left transition-colors duration-75
							{activeTab === tab.id
							? 'font-medium text-gray-900 dark:text-white bg-gray-100 dark:bg-white/6'
							: 'text-gray-500 hover:text-gray-700 dark:hover:text-gray-300'}"
						onclick={() => (activeTab = tab.id)}
					>
						<Icon name={tab.icon} size={14} />
						{tab.label}
					</button>
				{/each}
			{/if}
		</div>
	</nav>

	<div class="flex-1 overflow-y-auto min-h-0 p-4 md:px-5">
		{#if activeTab === 'general'}
			<General />
		{:else if activeTab === 'keyboard'}
			<Keyboard />
		{:else if activeTab === 'browser'}
			<Browser />
		{:else if activeTab === 'account'}
			<Account />
		{:else if activeTab === 'about'}
			<About />
		{:else if activeTab === 'users'}
			<Users />
		{:else if activeTab === 'connections'}
			<Connections />
		{:else if activeTab === 'models'}
			<Models />
		{:else if activeTab === 'messaging'}
			<Messaging />
		{:else if activeTab === 'gateway'}
			<Gateway />
		{:else if activeTab === 'audio'}
			<AudioSettings />
		{:else if activeTab === 'admin_settings'}
			<AdminSettings />
		{/if}
	</div>
</Modal>
