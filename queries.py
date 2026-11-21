"""Parameterized operations used by the Eversource DataBrowser client.

Only operation text belongs here. Never bundle captured variables, headers,
responses, or the raw research catalog.
"""

QUERIES = {
    'GetBillForecast': """
query GetBillForecast ($selectedAccount: ID) {
        billingAccountByAuthContext (selectedAccount: $selectedAccount) {
            urn
            billForecast {
                timeInterval
                currentDateTime
                estimatedUsage {
                    value
                    unit
                }
                estimatedUsageCharges {
                    currency
                    value
                }
                estimatedUsageChargesUpperBound {
                    currency
                    value
                }
                soFarUsage {
                    value
                    unit
                }
                soFarUsageCharges {
                    currency
                    value
                }
                priorYearUsage {
                    value
                    unit
                }
                priorYearUsageCharges {
                    currency
                    value
                }
                calculationFlags
                segments {
                    serviceAgreement {
                        mappedUtilityAccounts {
                            uuid
                        }
                        urn
                        utilityId
                        uuid
                        serviceType
                        isBudgetBilling
                        isSolar
                        solarTrueUpPeriods {
                            startDateTime
                            endDateTime
                        }
                        servicePointsConnection {
                            edges {
                                node {
                                    urn
                                    utilityId
                                    uuid
                                }
                            }
                        }
                    }
                    premise {
                        urn
                        utilityId
                        uuid
                    }
                    estimatedUsage {
                        value
                        unit
                    }
                    estimatedUsageCharges {
                        currency
                        value
                    }
                    estimatedUsageChargesUpperBound {
                        currency
                        value
                    }
                    soFarUsage {
                        value
                        unit
                    }
                    soFarUsageCharges {
                        currency
                        value
                    }
                    priorYearUsage {
                        value
                        unit
                    }
                    priorYearUsageCharges {
                        currency
                        value
                    }
                }
            }
        }
    }
""",
    'WDB_GetCostUsageReadsForBills': """
query WDB_GetCostUsageReadsForBills(
        $selectedAccount: ID,
        $customerURN: ID,
        $last: Int,
        $timeInterval: TimeInterval,
        $forceLegacyData: Boolean,
        $aliased: Boolean
    ) {
        billingAccountByAuthContext(
            selectedAccount: $selectedAccount,
            singlePremise: $customerURN,
            forceLegacyData: $forceLegacyData
        ) {
            urn
            bills(last: $last, during: $timeInterval, orderBy: ASCENDING, preserveDuplicateSegments: true) {
                urn
                timeInterval
                segments {
                    urn
                    usageInterval
                    serviceAgreement(aliased: $aliased) {
                        urn
                        uuid
                        serviceType
                    }
                    estimated
                    serviceQuantities {
                        unit
                        serviceQuantityIdentifier
                        utilityServiceQuantityIdentifier
                        serviceQuantity {
                            value
                        }
                    }
                    usageCharges {
                        value
                    }
                    currentAmount {
                        value
                    }
                    deferredNEMCharges {
                        value
                    }
                    totalNEMCharges {
                        value
                    }
                    energyPurchased {
                        value
                    }
                    energySold {
                        value
                    }
                    rolloverBalanceEarned {
                        value
                    }
                    rolloverBalanceUsed {
                        value
                    }
                    totalEnergyCosts {
                        value
                    }
                }
            }
        }
    }
""",
    'WDB_GetHourlyPricingReadsForDayAndHour': """
query WDB_GetHourlyPricingReadsForDayAndHour(
        $selectedAccount: ID,
        $customerURN: ID,
        $timeInterval: TimeInterval,
        $resolution: ReadResolution,
        $forceLegacyData: Boolean,
        $aliased: Boolean,
        $saUuid: String,
        $spUuid: String,
        $fillMissingIntervals: Boolean
    ){
        billingAccountByAuthContext(
            selectedAccount: $selectedAccount,
            singlePremise: $customerURN,
            forceLegacyData: $forceLegacyData
        ) {
            serviceAgreementsConnection(
                onlyActive: true
                aliased: $aliased
                matching: $saUuid
            ) {
                edges {
                    node {
                        uuid
                        serviceType
                        ratePlan {
                            code
                            currentRatePlan
                            rateTags
                            rateAttributes {
                                key
                                value
                            }
                            titleShort
                            descriptionShort
                        }
                        servicePointsConnection(matching: $spUuid) {
                            edges {
                                node  {
                                    serviceType
                                    utilityId
                                    uuid
                                    readStreams(timeInterval: $timeInterval, readResolution: $resolution, fillMissingIntervals: $fillMissingIntervals) {
                                        timeInterval
                                        rates {
                                            rateType
                                            tier
                                            costPerUnit {
                                                value
                                            }
                                            timeOfUsePeriod {
                                                label
                                                timeOfUse
                                                season
                                                times
                                            }
                                            activationDate
                                            inactivationDate
                                        }
                                        netUsage {
                                            serviceQuantityIdentifier
                                            unit
                                            isOpowerDerived
                                            registerId
                                            reads {
                                                readType
                                                timeInterval
                                                measuredAmount {
                                                    unit
                                                    value
                                                }
                                                monetaryAmount {
                                                    value
                                                }
                                                rebateAmount {
                                                    value
                                                }
                                                isPeakPeriod
                                                ratedReadComponents {
                                                    attributes {
                                                        key
                                                        value
                                                    }
                                                    measuredAmount {
                                                        value
                                                    }
                                                    monetaryAmount {
                                                        value
                                                    }
                                                    costPerUnit {
                                                        value
                                                    }
                                                }
                                            }
                                        }
                                        demand {
                                            serviceQuantityIdentifier
                                            unit
                                            isOpowerDerived
                                            registerId
                                            reads {
                                                readType
                                                timeInterval
                                                isMaximum
                                                measuredAmount {
                                                    unit
                                                    value
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
""",
    'WDB_GetRateStaticContent': """
query WDB_GetRateStaticContent(
        $selectedAccount: ID,
        $customerURN: ID,
        $forceLegacyData: Boolean,
        $aliased: Boolean,
        $saUuid: String,
    ) {
        billingAccountByAuthContext(
            selectedAccount: $selectedAccount,
            singlePremise: $customerURN,
            forceLegacyData: $forceLegacyData
        ) {
            serviceAgreementsConnection(
                onlyActive: true
                aliased: $aliased
                matching: $saUuid
            ) {
                edges {
                    node {
                        uuid
                        ratePlan {
                            code
                            title
                            additionalContent {
                                key
                                value
                            }
                            ratePlanCharacteristics {
                                id {
                                    key
                                    value
                                }
                                additionalContent {
                                    key
                                    value
                                }
                            }
                        }
                    }
                }
            }
        }
    }
""",
    'WDB_GetWeather': """
query WDB_GetWeather(
        $selectedAccount: ID,
        $customerURN: ID,
        $unit: TemperatureUnit,
        $timeInterval: [TimeInterval],
        $weatherResolution: WeatherResolutionType,
        $forceLegacyData: Boolean,
        $premiseUuid: String
    ) {
        billingAccountByAuthContext(
            selectedAccount: $selectedAccount,
            singlePremise: $customerURN,
            forceLegacyData: $forceLegacyData
        ) {
            premisesConnection (matching: $premiseUuid) {
                edges {
                    node {
                        uuid
                        weather(
                            weatherResolution: $weatherResolution,
                            intervals: $timeInterval,
                            temperatureUnit: $unit
                        ) {
                            timeInterval
                            maxTemperature {
                                value
                            }
                            meanTemperature {
                                value
                            }
                            minTemperature {
                                value
                            }
                        }
                    }
                }
            }
        }
    }
""",
    'WDH_GetAmiUsage': """
query WDH_GetAmiUsage(
        $timeInterval: TimeInterval,
        $selectedAccount: ID,
        $forceLegacyData: Boolean,
        $saUuid: String,
        $spUuid: String
    ){
        billingAccountByAuthContext(selectedAccount: $selectedAccount, forceLegacyData: $forceLegacyData) {
            urn
            uuid
            serviceAgreementsConnection(matching: $saUuid) {
                edges {
                    node {
                        urn
                        uuid
                        utilityId
                        servicePointsConnection(matching: $spUuid) {
                            edges {
                                node {
                                    urn
                                    uuid
                                    utilityId
                                    readStreams(timeInterval: $timeInterval) {
                                        timeInterval
                                        demand {
                                            serviceQuantityIdentifier
                                            registerId
                                            unit
                                            isOpowerDerived
                                            timeInterval
                                            reads {
                                                readType
                                                timeInterval
                                                isMaximum
                                                measuredAmount {
                                                    unit
                                                    value
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
""",
    'WUE_GenerateUsageExportFile': """
mutation WUE_GenerateUsageExportFile(
        $usageExportFileConfigurationInput: UsageExportFileConfigurationInput
    ) {
        generateUsageExportFile(
            usageExportFileConfigurationInput: $usageExportFileConfigurationInput
        ) {
            uuid
        }
    }
""",
    'WUE_GetExportJob': """
query WUE_GetExportJob(
        $jobUuid: ID!
    ) {
        exportJob(
            jobUuid: $jobUuid,
        ) {
            uuid
            result
            isRunning
            isFailed
            isFinished
        }
    }
""",
}
