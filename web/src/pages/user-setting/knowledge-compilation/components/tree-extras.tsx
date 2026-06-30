import {
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '@/components/ui/form';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { useFormContext } from 'react-hook-form';
import { useTranslation } from 'react-i18next';

interface TreeExtrasProps {
  pathPrefix?: string;
}

function _join(prefix: string | undefined, path: string): string {
  return prefix ? `${prefix}.${path}` : path;
}

/**
 * RAPTOR-style knobs (summarization prompt + max_token + threshold).
 * Only mounted when `kind === 'tree'`. Mirrors the artifact-extras
 * pattern so the conditional rendering tree stays clean for other
 * kinds.
 */
export function TreeExtras({ pathPrefix }: TreeExtrasProps) {
  // Form context widened — same reason as in ArtifactExtras.
  const form = useFormContext<any>();
  const { t } = useTranslation();

  const promptPath = _join(pathPrefix, 'raptor.prompt');
  const maxTokenPath = _join(pathPrefix, 'raptor.max_token');
  const thresholdPath = _join(pathPrefix, 'raptor.threshold');
  const rechunkPath = _join(pathPrefix, 'raptor.rechunk');

  return (
    <section className="space-y-3">
      <h3 className="text-sm font-medium">
        {t('knowledgeCompilation.treeSectionTitle')}
      </h3>

      <FormField
        control={form.control}
        name={promptPath as any}
        render={({ field }) => (
          <FormItem>
            <FormLabel>{t('knowledgeCompilation.treePromptLabel')}</FormLabel>
            <FormControl>
              <Textarea
                {...field}
                rows={6}
                placeholder={t('knowledgeCompilation.treePromptPlaceholder')}
                className="font-mono text-sm"
              />
            </FormControl>
            <FormMessage />
          </FormItem>
        )}
      />

      <div className="grid grid-cols-2 gap-3">
        <FormField
          control={form.control}
          name={maxTokenPath as any}
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                {t('knowledgeCompilation.treeMaxTokenLabel')}
              </FormLabel>
              <FormControl>
                <Input
                  type="number"
                  min={1}
                  max={8192}
                  step={1}
                  {...field}
                  value={field.value ?? 512}
                  onChange={(e) => field.onChange(Number(e.target.value))}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />

        <FormField
          control={form.control}
          name={thresholdPath as any}
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                {t('knowledgeCompilation.treeThresholdLabel')}
              </FormLabel>
              <FormControl>
                <Input
                  type="number"
                  min={0}
                  max={1}
                  step={0.01}
                  {...field}
                  value={field.value ?? 0.1}
                  onChange={(e) => field.onChange(Number(e.target.value))}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
      </div>

      {/* Re-chunk toggle. When enabled, the backend merges each leaf
          cluster's source chunks into a single replacement chunk and
          marks the originals unavailable (``available_int=0``). Off
          by default. Only one tree template per group may enable
          this; the group editor blocks save otherwise. */}
      <FormField
        control={form.control}
        name={rechunkPath as any}
        render={({ field }) => (
          <FormItem className="flex flex-row items-start justify-between gap-3 rounded-md border border-border-button p-3">
            <div className="flex flex-col gap-1">
              <FormLabel className="m-0">
                {t('knowledgeCompilation.rechunkLabel')}
              </FormLabel>
              <p className="text-xs text-text-secondary">
                {t('knowledgeCompilation.rechunkDescription')}
              </p>
            </div>
            <FormControl>
              <Switch
                checked={Boolean(field.value)}
                onCheckedChange={field.onChange}
              />
            </FormControl>
          </FormItem>
        )}
      />
    </section>
  );
}
