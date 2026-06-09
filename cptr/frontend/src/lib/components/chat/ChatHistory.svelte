<script lang="ts">
	import type { ChatInfo } from '$lib/apis/chat';
	import DropdownMenu from '../DropdownMenu.svelte';
	import Pagination from '../common/Pagination.svelte';
	import Spinner from '../common/Spinner.svelte';

	interface Props {
		chats: ChatInfo[];
		onopen: (id: string) => void;
		ondelete: (id: string) => void;
		page?: number;
		totalPages?: number;
		perPage?: number;
		onpagechange?: (page: number) => void;
		sortBy?: 'title' | 'updated_at';
		sortDir?: 'asc' | 'desc';
		onsort?: (field: 'title' | 'updated_at') => void;
	}
	let {
		chats,
		onopen,
		ondelete,
		page = 1,
		totalPages = 1,
		perPage = 10,
		onpagechange,
		sortBy = 'updated_at',
		sortDir = 'desc',
		onsort
	}: Props = $props();

	let menuChatId = $state<string | null>(null);
	let menuAnchor = $state<HTMLElement | null>(null);

	function openMenu(e: MouseEvent, chatId: string) {
		e.stopPropagation();
		menuChatId = chatId;
		menuAnchor = e.currentTarget as HTMLElement;
	}

	function closeMenu() {
		menuChatId = null;
		menuAnchor = null;
	}

	function formatTime(ts: number): string {
		const d = new Date(ts);
		const now = new Date();
		const diffMs = now.getTime() - d.getTime();
		const diffM = Math.floor(diffMs / 60000);
		if (diffM < 1) return 'Just now';
		if (diffM < 60) return `${diffM}m ago`;
		const diffH = Math.floor(diffM / 60);
		if (diffH < 24) return `${diffH}h ago`;
		const diffD = Math.floor(diffH / 24);
		if (diffD < 7) return `${diffD}d ago`;
		return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
</script>

{#snippet chevronUp()}
	<svg
		class="w-2 h-2"
		viewBox="0 0 10 6"
		fill="none"
		stroke="currentColor"
		stroke-width="2"
		stroke-linecap="round"
		stroke-linejoin="round"
	>
		<path d="M1 5L5 1L9 5" />
	</svg>
{/snippet}

{#snippet chevronDown()}
	<svg
		class="w-2 h-2"
		viewBox="0 0 10 6"
		fill="none"
		stroke="currentColor"
		stroke-width="2"
		stroke-linecap="round"
		stroke-linejoin="round"
	>
		<path d="M1 1L5 5L9 1" />
	</svg>
{/snippet}

{#if chats.length > 0}
	<div class="w-full mt-4 pt-2">
		<!-- Sort header -->
		{#if onsort}
			<div
				class="flex items-center text-[10px] font-medium text-gray-400 dark:text-gray-600 mb-1 px-2 select-none"
			>
				<button
					class="flex-1 flex items-center gap-1 hover:text-gray-600 dark:hover:text-gray-400 transition-colors"
					onclick={() => onsort?.('title')}
				>
					Title
					{#if sortBy === 'title'}
						{#if sortDir === 'asc'}
							{@render chevronUp()}
						{:else}
							{@render chevronDown()}
						{/if}
					{:else}
						<span class="invisible">{@render chevronUp()}</span>
					{/if}
				</button>
				<button
					class="shrink-0 flex items-center gap-1 hover:text-gray-600 dark:hover:text-gray-400 transition-colors"
					onclick={() => onsort?.('updated_at')}
				>
					Updated
					{#if sortBy === 'updated_at'}
						{#if sortDir === 'asc'}
							{@render chevronUp()}
						{:else}
							{@render chevronDown()}
						{/if}
					{:else}
						<span class="invisible">{@render chevronUp()}</span>
					{/if}
				</button>
			</div>
		{/if}

		{#each chats as chat (chat.id)}
			<!-- svelte-ignore a11y_no_static_element_interactions -->
			<div
				class="group flex items-center gap-2 w-full h-7 px-2 rounded-lg hover:bg-gray-100 dark:hover:bg-white/4 transition-colors duration-75 cursor-pointer"
				role="button"
				tabindex="0"
				onclick={() => onopen(chat.id)}
				onkeydown={(e) => {
					if (e.key === 'Enter') onopen(chat.id);
				}}
			>
				{#if chat.is_active}
					<Spinner size={10} borderWidth={1.5} class="opacity-50" />
				{/if}
				<span class="flex-1 text-xs text-gray-500 dark:text-gray-500 truncate">{chat.title}</span>
				<span class="text-[10px] text-gray-300 dark:text-gray-700 shrink-0"
					>{formatTime(chat.updated_at)}</span
				>
				<button
					class="flex items-center justify-center w-5 h-5 rounded shrink-0 text-gray-300 dark:text-gray-700 hover:text-gray-500 dark:hover:text-gray-400 transition-all duration-75"
					onclick={(e) => openMenu(e, chat.id)}
					aria-label="Chat options"
				>
					<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor">
						<circle cx="3" cy="8" r="1.5" />
						<circle cx="8" cy="8" r="1.5" />
						<circle cx="13" cy="8" r="1.5" />
					</svg>
				</button>
			</div>
		{/each}
		{#if onpagechange}
			<Pagination {page} {totalPages} {onpagechange} />
		{/if}
	</div>
{/if}

{#if menuChatId && menuAnchor}
	<DropdownMenu
		anchor={menuAnchor}
		align="end"
		items={[
			{
				label: 'Delete',
				icon: 'trash',
				onclick: () => {
					if (menuChatId) ondelete(menuChatId);
				}
			}
		]}
		onclose={closeMenu}
	/>
{/if}

