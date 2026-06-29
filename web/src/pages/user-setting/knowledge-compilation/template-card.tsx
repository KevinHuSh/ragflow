import { CardContainer } from '@/components/card-container';
import { ConfirmDeleteDialog } from '@/components/confirm-delete-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { CompilationTemplateGroup } from '@/interfaces/database/compilation-template';
import { Pencil, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';

interface GroupCardProps {
  data: CompilationTemplateGroup;
  onEdit: (id: string) => void;
  onDelete: (id: string) => void;
}

/**
 * Single card in the template-groups list. Header shows name +
 * description; body shows the scope badge and one chip per child
 * template kind. Clicking anywhere on the card opens the editor.
 */
export function TemplateGroupCard({ data, onEdit, onDelete }: GroupCardProps) {
  const { t } = useTranslation();

  return (
    <Card
      role="button"
      tabIndex={0}
      className="cursor-pointer hover:border-primary"
      onClick={() => onEdit(data.id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onEdit(data.id);
        }
      }}
    >
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div className="flex flex-col gap-1 min-w-0">
          <CardTitle className="truncate" title={data.name}>
            {data.name}
          </CardTitle>
          <CardDescription className="truncate" title={data.description}>
            {data.description || t('knowledgeCompilation.noDescription')}
          </CardDescription>
        </div>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-1.5">
        <Badge variant="secondary">
          {t(`knowledgeCompilation.scope.${data.scope}`)}
        </Badge>
        {data.templates.map((tpl) => (
          <Badge key={tpl.id} variant="outline" title={tpl.name}>
            {t(`knowledgeCompilation.kind.${tpl.kind}`)}
          </Badge>
        ))}
      </CardContent>
      <CardFooter
        className="flex justify-end gap-2"
        onClick={(e) => e.stopPropagation()}
      >
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onEdit(data.id)}
          aria-label={t('common.edit')}
        >
          <Pencil className="size-3.5" />
          {t('common.edit')}
        </Button>
        <ConfirmDeleteDialog
          onOk={() => onDelete(data.id)}
          title={t('common.delete')}
          content={{
            title: t('knowledgeCompilation.confirmDelete'),
            node: <span className="font-medium">{data.name}</span>,
          }}
        >
          <Button variant="danger" size="sm" aria-label={t('common.delete')}>
            <Trash2 className="size-3.5" />
            {t('common.delete')}
          </Button>
        </ConfirmDeleteDialog>
      </CardFooter>
    </Card>
  );
}

interface GroupCardListProps {
  items: CompilationTemplateGroup[];
  onEdit: (id: string) => void;
  onDelete: (id: string) => void;
}

export function TemplateGroupCardList({
  items,
  onEdit,
  onDelete,
}: GroupCardListProps) {
  return (
    <CardContainer>
      {items.map((item) => (
        <TemplateGroupCard
          key={item.id}
          data={item}
          onEdit={onEdit}
          onDelete={onDelete}
        />
      ))}
    </CardContainer>
  );
}
