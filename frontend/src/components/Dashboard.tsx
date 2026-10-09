import React, { useState, useEffect } from "react";
import { RevenueSummary } from "./RevenueSummary";
import { SecureAPI } from "../lib/secureApi";

interface Property {
  id: string;
  name: string;
  timezone?: string;
}

const Dashboard: React.FC = () => {
  const [properties, setProperties] = useState<Property[]>([]);
  const [selectedProperty, setSelectedProperty] = useState<string>('');
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string>('');
  const [selectedMonth, setSelectedMonth] = useState<number>(3); // March (Default per assignment)
  const [selectedYear, setSelectedYear] = useState<number>(2024);

  useEffect(() => {
    let isMounted = true;

    const fetchProperties = async () => {
      setLoading(true);
      setError('');
      try {
        const response = await SecureAPI.getProperties();
        const propList = response.data || [];
        if (isMounted) {
          setProperties(propList);
          if (propList.length > 0) {
            setSelectedProperty(propList[0].id);
          }
        }
      } catch (err: any) {
        if (isMounted) {
          console.error("Failed to fetch tenant properties:", err);
          setError("Failed to load properties for your organization");
        }
      } finally {
        if (isMounted) {
          setLoading(false);
        }
      }
    };

    fetchProperties();

    return () => {
      isMounted = false;
    };
  }, []);

  return (
    <div className="p-4 lg:p-6 min-h-full">
      <div className="max-w-7xl mx-auto">
        <h1 className="text-2xl font-bold mb-6 text-gray-900">Property Management Dashboard</h1>

        <div className="bg-white rounded-lg shadow-sm border border-gray-200 p-4 lg:p-6">
          <div className="mb-6">
            <div className="flex flex-col sm:flex-row sm:justify-between sm:items-start gap-4">
              <div>
                <h2 className="text-lg lg:text-xl font-medium text-gray-900 mb-2">Revenue Overview</h2>
                <p className="text-sm lg:text-base text-gray-600">
                  Monthly performance insights for your properties
                </p>
              </div>
              
              {/* Selectors */}
              <div className="flex flex-wrap sm:flex-nowrap gap-3 items-end">
                {/* Month / Year Selector */}
                <div className="flex flex-col">
                  <label className="text-xs font-medium text-gray-700 mb-1">Period</label>
                  <select
                    value={`${selectedYear}-${selectedMonth}`}
                    onChange={(e) => {
                      const [year, month] = e.target.value.split('-').map(Number);
                      setSelectedYear(year);
                      setSelectedMonth(month);
                    }}
                    className="block w-full sm:w-auto px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 text-sm"
                  >
                    <option value="2024-3">March 2024</option>
                    <option value="2024-2">February 2024</option>
                    <option value="2024-1">January 2024</option>
                    <option value="2024-4">April 2024</option>
                  </select>
                </div>

                {/* Property Selector */}
                <div className="flex flex-col">
                  <label className="text-xs font-medium text-gray-700 mb-1">Select Property</label>
                  {loading ? (
                    <div className="h-9 w-48 bg-gray-100 rounded-md animate-pulse"></div>
                  ) : properties.length === 0 ? (
                    <div className="text-sm text-gray-500 py-2">No properties found</div>
                  ) : (
                    <select
                      value={selectedProperty}
                      onChange={(e) => setSelectedProperty(e.target.value)}
                      className="block w-full sm:w-auto min-w-[200px] px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500 text-sm"
                    >
                      {properties.map((property) => (
                        <option key={property.id} value={property.id}>
                          {property.name} ({property.id})
                        </option>
                      ))}
                    </select>
                  )}
                </div>
              </div>
            </div>
          </div>

          {error ? (
            <div className="p-4 text-red-500 bg-red-50 rounded-lg">{error}</div>
          ) : selectedProperty ? (
            <div className="space-y-6">
              <RevenueSummary 
                propertyId={selectedProperty} 
                month={selectedMonth}
                year={selectedYear}
              />
            </div>
          ) : (
            <div className="text-center py-12 text-gray-500">
              {loading ? "Loading properties..." : "No properties available for this account."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

export default Dashboard;
