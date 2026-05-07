import { useTranslation } from 'react-i18next';
import { Languages } from 'lucide-react';

export function LanguageSwitcher() {
  const { i18n, t } = useTranslation();
  const isZh = i18n.language.startsWith('zh');

  const toggleLanguage = () => {
    const nextLng = isZh ? 'en' : 'zh';
    i18n.changeLanguage(nextLng);
  };

  return (
    <button
      onClick={toggleLanguage}
      className="inline-flex items-center justify-center gap-2 px-0 py-2 bg-white border border-gray-200 rounded-xl font-bold text-xs uppercase text-gray-600 hover:text-indigo-600 hover:border-indigo-200 transition-all shadow-sm group w-20"
      title={t('common.languageSwitcher.title')}
    >
      <Languages size={14} className="text-gray-400 group-hover:text-indigo-500 transition-colors" />
      <span className="w-8 text-center">
        {isZh ? t('common.languageSwitcher.switchToEnglish') : t('common.languageSwitcher.switchToChinese')}
      </span>
    </button>
  );
}
