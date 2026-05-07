import { Routes, Route, Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ProjectList } from './components/ProjectList';
import { ProjectDetail } from './components/ProjectDetail';
import { ExpertCenter } from './components/Management';
import { ProjectConfig } from './components/ProjectConfig';

function App() {
  const { t } = useTranslation();

  return (
    <div className="min-h-screen">
      <Routes>
        <Route path="/" element={<ProjectList />} />
        <Route path="/projects/:id" element={<ProjectDetail />} />
        <Route path="/projects/:id/config" element={<ProjectConfig />} />
        <Route path="/management" element={<ExpertCenter />} />
        <Route path="/expert-center" element={<ExpertCenter />} />
        <Route
          path="*"
          element={
            <div className="min-h-screen flex items-center justify-center p-6">
              <div className="bg-white rounded-lg border border-gray-200 shadow-sm p-6 text-center max-w-md w-full">
                <h1 className="text-xl font-bold text-gray-800 mb-2">{t('app.notFound.title')}</h1>
                <p className="text-sm text-gray-500 mb-4">{t('app.notFound.description')}</p>
                <Link to="/" className="inline-flex px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700">
                  {t('app.notFound.action')}
                </Link>
              </div>
            </div>
          }
        />
      </Routes>
    </div>
  );
}

export default App;
