import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  useCreateCompilationTemplateGroup,
  useFetchCompilationTemplateGroup,
  useListCompilationTemplateGroups,
  useUpdateCompilationTemplateGroup,
} from '@/hooks/use-compilation-template-request';
import { useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { EditTemplateGroupForm } from './edit-template-group-form';

interface EditTemplateGroupDialogProps {
  id: string;
  hideModal: () => void;
}

/**
 * Modal wrapper around {@link EditTemplateGroupForm}. Drives the same
 * create/update mutation pair as the previous per-template dialog;
 * the only difference is the payload shape (group + embedded
 * children).
 */
export function EditTemplateGroupDialog({
  id,
  hideModal,
}: EditTemplateGroupDialogProps) {
  const { t } = useTranslation();
  const { data: initial, loading: loadingInitial } =
    useFetchCompilationTemplateGroup(id);
  const { createCompilationTemplateGroup, loading: creating } =
    useCreateCompilationTemplateGroup();
  const { updateCompilationTemplateGroup, loading: updating } =
    useUpdateCompilationTemplateGroup();
  const { data: savedGroups } = useListCompilationTemplateGroups();

  const isEditing = Boolean(id);
  const loading = loadingInitial || creating || updating;
  const [isDirty, setIsDirty] = useState(false);
  const [confirmCloseVisible, setConfirmCloseVisible] = useState(false);

  const requestClose = useCallback(() => {
    if (isDirty && !loading) {
      setConfirmCloseVisible(true);
      return;
    }
    hideModal();
  }, [hideModal, isDirty, loading]);

  const confirmClose = useCallback(() => {
    setConfirmCloseVisible(false);
    hideModal();
  }, [hideModal]);

  const handleSubmit = useCallback(
    async (payload: {
      name: string;
      description: string;
      templates: any[];
    }) => {
      const code = isEditing
        ? await updateCompilationTemplateGroup({ id, ...payload })
        : await createCompilationTemplateGroup(payload);
      if (code === 0) {
        setIsDirty(false);
        hideModal();
      }
    },
    [
      isEditing,
      id,
      updateCompilationTemplateGroup,
      createCompilationTemplateGroup,
      hideModal,
    ],
  );

  return (
    <>
      <Dialog open onOpenChange={(open) => !open && requestClose()}>
        <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>
              {isEditing
                ? t('knowledgeCompilation.editTemplateGroup')
                : t('knowledgeCompilation.addTemplateGroup')}
            </DialogTitle>
          </DialogHeader>
          {isEditing && loadingInitial ? (
            <p className="p-6 text-sm text-text-secondary">
              {t('common.loading')}
            </p>
          ) : (
            <EditTemplateGroupForm
              initial={isEditing ? initial : undefined}
              savedGroups={savedGroups.groups}
              onSubmit={handleSubmit}
              onCancel={requestClose}
              onDirtyChange={setIsDirty}
              loading={loading}
            />
          )}
        </DialogContent>
      </Dialog>
      <AlertDialog
        open={confirmCloseVisible}
        onOpenChange={setConfirmCloseVisible}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {t('knowledgeCompilation.confirmCloseTitle')}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t('knowledgeCompilation.confirmCloseBody')}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction onClick={confirmClose}>
              {t('knowledgeCompilation.discardAndClose')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
