<script lang="ts">
	import { goto } from '$app/navigation';
	import { addWorkspace } from '$lib/stores';
	import { getWelcome } from '$lib/apis/state';
	import { listDir } from '$lib/apis/files';
	import Icon from './Icon.svelte';
	import { t } from '$lib/i18n';

	interface Props {
		onclose: () => void;
	}

	interface DirEntry {
		name: string;
		type: 'directory' | 'file' | 'symlink';
		modified: string | null;
	}

	let { onclose }: Props = $props();

	let currentPath = $state('~');
	let directories = $state<DirEntry[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let selectedIndex = $state(-1);
	let showHidden = $state(false);
	let listEl: HTMLDivElement | undefined = $state();
	let history = $state<string[]>([]);

	// ── Editable path bar ───────────────────────────────────────
	let editingPath = $state(false);
	let pathInputValue = $state('');
	let pathInputEl: HTMLInputElement | undefined = $state();
	let pathValid = $state<'idle' | 'checking' | 'valid' | 'invalid'>('idle');
	let validateTimer: ReturnType<typeof setTimeout> | null = null;
	let tabHint = $state('');

	const filteredDirs = $derived.by(() => {
		if (showHidden) return directories;
		return directories.filter((d) => !d.name.startsWith('.'));
	});

	const breadcrumbs = $derived.by(() => {
		if (!currentPath || currentPath === '/') return [{ name: '/', path: '/' }];
		const parts = currentPath.split('/').filter(Boolean);
		const segs: { name: string; path: string }[] = [{ name: '/', path: '/' }];
		let built = '';
		for (const p of parts) {
			built += '/' + p;
			segs.push({ name: p, path: built });
		}
		return segs;
	});

	$effect(() => {
		selectedIndex = -1;
		history = [];
		getWelcome().then(d => {
			const home = d.suggestions?.[0]?.path || '/';
			fetchDirectories(home);
		}).catch(() => fetchDirectories('/'));
	});

	$effect(() => {
		if (filteredDirs) selectedIndex = -1;
	});

	$effect(() => {
		if (editingPath && pathInputEl) {
			requestAnimationFrame(() => {
				pathInputEl?.focus();
				pathInputEl?.select();
			});
		}
	});

	async function fetchDirectories(path: string) {
		loading = true;
		error = null;
		try {
			const data = await listDir(path);
			directories = data.entries.filter((e: DirEntry) => e.type === 'directory');
			currentPath = data.path;
		} catch (e: any) {
			error = e.message || $t('files.failedToLoad');
			directories = [];
		} finally {
			loading = false;
		}
	}

	function navigateTo(dirName: string) {
		history = [...history, currentPath];
		const newPath = currentPath === '/' ? `/${dirName}` : `${currentPath}/${dirName}`;
		fetchDirectories(newPath);
	}

	function navigateToPath(path: string) {
		history = [...history, currentPath];
		fetchDirectories(path);
	}

	function goBack() {
		if (history.length === 0) return;
		const prev = history[history.length - 1];
		history = history.slice(0, -1);
		fetchDirectories(prev);
	}

	function selectCurrent() {
		addWorkspace(currentPath);
		goto(`/?workspace=${encodeURIComponent(currentPath)}`);
		onclose();
	}

	// ── Path editing ────────────────────────────────────────────
	function startEditing() {
		pathInputValue = currentPath;
		editingPath = true;
		pathValid = 'idle';
		tabHint = '';
	}

	function cancelEditing() {
		editingPath = false;
		pathInputValue = '';
		pathValid = 'idle';
		tabHint = '';
		if (validateTimer) { clearTimeout(validateTimer); validateTimer = null; }
	}

	function confirmPath() {
		const val = pathInputValue.trim();
		if (!val) { cancelEditing(); return; }
		editingPath = false;
		pathValid = 'idle';
		tabHint = '';
		navigateToPath(val);
	}

	function onPathInput() {
		tabHint = '';
		if (validateTimer) clearTimeout(validateTimer);
		const val = pathInputValue.trim();
		if (!val) { pathValid = 'idle'; return; }
		pathValid = 'checking';
		validateTimer = setTimeout(async () => {
			try {
				await listDir(val);
				pathValid = 'valid';
			} catch {
				pathValid = 'invalid';
			}
		}, 300);
	}

	async function tabComplete() {
		const val = pathInputValue.trim();
		if (!val) return;
		const lastSlash = val.lastIndexOf('/');
		if (lastSlash < 0) return;
		const parent = val.substring(0, lastSlash) || '/';
		const partial = val.substring(lastSlash + 1).toLowerCase();
		try {
			const data = await listDir(parent);
			const dirs = data.entries.filter((e: DirEntry) => e.type === 'directory').map((e: DirEntry) => e.name);
			if (partial) {
				const matches = dirs.filter((n: string) => n.toLowerCase().startsWith(partial));
				if (matches.length === 1) {
					pathInputValue = parent === '/' ? `/${matches[0]}` : `${parent}/${matches[0]}`;
					tabHint = '';
					onPathInput();
				} else if (matches.length > 1) {
					const cp = commonPrefix(matches);
					if (cp.length > partial.length) {
						pathInputValue = parent === '/' ? `/${cp}` : `${parent}/${cp}`;
					}
					tabHint = `${matches.length} matches`;
					onPathInput();
				}
			} else if (dirs.length > 0) {
				tabHint = `${dirs.length} dirs`;
			}
		} catch {}
	}

	function commonPrefix(strs: string[]): string {
		if (strs.length === 0) return '';
		let p = strs[0];
		for (let i = 1; i < strs.length; i++) {
			while (!strs[i].toLowerCase().startsWith(p.toLowerCase())) {
				p = p.slice(0, -1);
				if (!p) return '';
			}
		}
		return p;
	}

	function handlePathKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); confirmPath(); }
		else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelEditing(); }
		else if (e.key === 'Tab') { e.preventDefault(); e.stopPropagation(); tabComplete(); }
	}

	function handleKeydown(e: KeyboardEvent) {
		if (editingPath) return;
		switch (e.key) {
			case 'Escape': onclose(); break;
			case 'ArrowDown':
				e.preventDefault();
				selectedIndex = Math.min(selectedIndex + 1, filteredDirs.length - 1);
				scrollSelectedIntoView();
				break;
			case 'ArrowUp':
				e.preventDefault();
				selectedIndex = Math.max(selectedIndex - 1, -1);
				scrollSelectedIntoView();
				break;
			case 'Enter':
				e.preventDefault();
				if (selectedIndex >= 0 && selectedIndex < filteredDirs.length) {
					navigateTo(filteredDirs[selectedIndex].name);
				}
				break;
			case 'Backspace':
				if (history.length > 0) { e.preventDefault(); goBack(); }
				break;
		}
	}

	function scrollSelectedIntoView() {
		requestAnimationFrame(() => {
			const el = listEl?.querySelector(`[data-index="${selectedIndex}"]`);
			el?.scrollIntoView({ block: 'nearest' });
		});
	}

	const currentFolderName = $derived.by(() => {
		const parts = currentPath.split('/').filter(Boolean);
		return parts.length > 0 ? parts[parts.length - 1] : '/';
	});
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="fixed inset-0 bg-black/50 z-[100] flex items-center justify-center" onmousedown={onclose} onkeydown={() => {}}>
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div
		class="w-full max-w-[560px] bg-white dark:bg-[#111] dark:border dark:border-white/8 rounded-3xl max-md:rounded-none overflow-hidden shadow-2xl flex flex-col"
		style="max-height: min(520px, 85vh);"
		onmousedown={(e) => e.stopPropagation()}
		onkeydown={() => {}}
	>
		<!-- Path bar -->
		<div class="flex items-center gap-2 px-4 h-11 border-b border-gray-200 dark:border-white/6 shrink-0">
			{#if history.length > 0}
				<button
					class="flex items-center justify-center w-6 h-6 rounded text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 transition-colors duration-100 shrink-0"
					onclick={goBack}
					aria-label={$t('directory.goBack')}
				>
					<Icon name="chevron-left" size={14} />
				</button>
			{/if}

			{#if editingPath}
				<div class="flex items-center gap-2 flex-1 min-w-0">
					<input
						bind:this={pathInputEl}
						bind:value={pathInputValue}
						type="text"
						class="flex-1 border-none outline-none bg-transparent text-xs text-gray-900 dark:text-white font-mono placeholder:text-gray-400"
						oninput={onPathInput}
						onkeydown={handlePathKeydown}
						onblur={() => { requestAnimationFrame(() => { if (editingPath) cancelEditing(); }); }}
						spellcheck="false"
						autocomplete="off"
						placeholder={$t('directory.typePath')}
					/>
					{#if pathValid === 'checking'}
						<div class="w-3 h-3 border-[1.5px] border-gray-300 border-t-gray-600 dark:border-gray-700 dark:border-t-gray-400 rounded-full animate-spin shrink-0"></div>
					{:else if pathValid === 'valid'}
						<span class="text-green-500 shrink-0 flex"><Icon name="check" size={12} /></span>
					{:else if pathValid === 'invalid'}
						<span class="text-red-400 shrink-0 flex"><Icon name="xmark" size={12} /></span>
					{/if}
					{#if tabHint}
						<span class="text-[10px] text-gray-400 whitespace-nowrap shrink-0">{tabHint}</span>
					{/if}
					<span class="text-[10px] font-mono font-medium px-1.5 py-0.5 rounded bg-gray-100 dark:bg-white/6 text-gray-400 shrink-0">TAB</span>
				</div>
			{:else}
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div
					class="group flex items-center gap-0.5 flex-1 min-w-0 cursor-text rounded-md px-1 -mx-1 transition-colors duration-100 hover:bg-gray-50 dark:hover:bg-white/4"
					role="button"
					tabindex="0"
					onclick={startEditing}
					onkeydown={(e) => { if (e.key === 'Enter') startEditing(); }}
				>
					{#each breadcrumbs as seg, i (seg.path)}
						{#if i > 1}<span class="text-gray-300 dark:text-gray-600 text-[11px] font-mono">/</span>{/if}
						{#if i === breadcrumbs.length - 1}
							<span class="text-[11px] font-mono font-medium text-gray-900 dark:text-white whitespace-nowrap overflow-hidden text-ellipsis">{seg.name}</span>
						{:else}
							<!-- svelte-ignore a11y_no_static_element_interactions -->
							<span
								class="text-[11px] font-mono text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 whitespace-nowrap shrink-0 cursor-pointer rounded px-0.5 hover:bg-gray-200 dark:hover:bg-white/8 transition-colors duration-75"
								role="button"
								tabindex="-1"
								onclick={(e) => { e.stopPropagation(); navigateToPath(seg.path); }}
								onkeydown={() => {}}
							>{seg.name}</span>
						{/if}
					{/each}
					<span class="ml-1 text-gray-300 dark:text-gray-700 opacity-0 group-hover:opacity-100 transition-opacity duration-150 shrink-0 flex items-center">
						<Icon name="pencil" size={10} />
					</span>
				</div>
			{/if}

			<label class="flex items-center gap-1.5 cursor-pointer select-none shrink-0">
				<input type="checkbox" bind:checked={showHidden} class="w-3 h-3 rounded accent-gray-500" />
				<span class="text-[10px] text-gray-400">{$t('directory.hidden')}</span>
			</label>
		</div>

		<!-- Directory listing -->
		<div bind:this={listEl} class="flex-1 overflow-y-auto p-1 min-h-0">
			{#if loading}
				<div class="flex items-center justify-center py-8">
					<div class="w-4 h-4 border-2 border-gray-300 border-t-gray-600 dark:border-gray-700 dark:border-t-gray-400 rounded-full animate-spin"></div>
				</div>
			{:else if error}
				<div class="flex flex-col items-center justify-center gap-2 py-8 text-center">
					<p class="text-xs text-red-400">{error}</p>
					<button
						class="text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 px-3 py-1 rounded-lg bg-gray-100 dark:bg-white/6 transition-colors duration-100"
						onclick={() => fetchDirectories(currentPath)}
					>{$t('directory.retry')}</button>
				</div>
			{:else if filteredDirs.length === 0}
				<div class="flex flex-col items-center justify-center py-8">
					<p class="text-xs text-gray-400 dark:text-gray-600">{$t('directory.noSubdirectories')}</p>
				</div>
			{:else}
				{#each filteredDirs as dir, i (dir.name)}
					<button
						data-index={i}
						class="flex items-center gap-2 w-full h-7 px-2 rounded-lg text-left transition-colors duration-75
							{i === selectedIndex
								? 'bg-gray-100 text-gray-900 dark:bg-white/6 dark:text-white'
								: 'text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/4'}"
						onclick={() => navigateTo(dir.name)}
						ondblclick={(e) => { e.stopPropagation(); const fullPath = currentPath === '/' ? `/${dir.name}` : `${currentPath}/${dir.name}`; addWorkspace(fullPath); goto(`/?workspace=${encodeURIComponent(fullPath)}`); onclose(); }}
						onmouseenter={() => { selectedIndex = i; }}
					>
						<Icon name="folder" size={14} class="shrink-0 text-gray-400" />
						<span class="flex-1 truncate text-xs">{dir.name}</span>
						<Icon name="chevron-right" size={12} class="shrink-0 text-gray-300 dark:text-gray-700" />
					</button>
				{/each}
			{/if}
		</div>

		<!-- Footer -->
		<div class="flex items-center gap-2 px-3 py-2 border-t border-gray-200 dark:border-white/6 shrink-0">
			<span class="flex-1 text-[11px] text-gray-400 dark:text-gray-600 font-mono truncate min-w-0" title={currentPath}>{currentPath}</span>
			<button
				class="text-xs font-medium text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 px-3 py-1.5 rounded-lg transition-colors duration-100 shrink-0"
				onclick={onclose}
				>{$t('directory.cancel')}</button>
			<button
				class="text-xs font-medium text-white dark:text-black bg-gray-900 dark:bg-white hover:bg-gray-800 dark:hover:bg-gray-200 px-3 py-1.5 rounded-lg transition-colors duration-100 shrink-0"
				onclick={selectCurrent}
				>{$t('directory.open', { name: currentFolderName })}</button>
		</div>
	</div>
</div>
